"""skmem-pg migration runner (card OPS1.3, epic eae727d0).

Applies the numbered, additive, idempotent skmem-pg forward migrations to an
ALREADY-RUNNING instance. This is the interface the skos provisioner (OPS1.2)
and existing live nodes call; a FRESH `docker compose up` instead auto-applies
the same migrations at first boot via ``deploy/skmem-pg/initdb/00-run-init.sh``.
Both read the SAME ordered manifest (``skmemory/pg/migrations.txt``), so the
fresh-boot and live-node paths can never drift.

Design notes:

* Pure plan first, side effects second. ``build_migrate_plan`` and
  ``build_roles_plan`` return fully-formed command plans (lists of argv +
  stdin) without touching anything, so the ordering + idempotence + pre-dump +
  verify contract is testable with no database and no psql present (mirroring
  the structural test for ``03-ops-namespace.sql``). ``run_plan`` executes.
* Two transports: ``docker exec -i <container> psql`` (default, matches the
  README apply command verbatim) or a libpq ``--dsn``. The runner shells out to
  psql/pg_dump; it never imports a driver, so it works wherever those binaries
  live (or is fully exercised in dry-run without them).
* Fail-safe: every apply runs with ``ON_ERROR_STOP=1``; each migration is
  itself one guarded transaction, so a re-run is a no-op and a partial apply
  cannot occur. ``--pre-dump`` takes ``pg_dump -Fc`` insurance first.
* Secrets never hit the repo, argv, or logs. Role passwords are read from the
  environment (populated from skvault via the documented env drop-in) and fed
  to psql on STDIN only; dry-run + error output redact them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

# --------------------------------------------------------------------------- #
# Locations                                                                    #
# --------------------------------------------------------------------------- #

# The SQL lives INSIDE the package (package data) so a wheel install has it; the
# compose initdb wrapper mounts this same directory. One copy, no drift.
DEPLOY_DIR = Path(__file__).resolve().parent / "pg"
MANIFEST = DEPLOY_DIR / "migrations.txt"

# Login-role -> group-role binding + the env var each password is read from.
# Passwords come from skvault, exported through ~/.config/environment.d/skbrain.conf
# (see deploy/skmem-pg/README.md "Role login binding"). Never hardcoded here.
ROLE_BINDINGS: dict[str, dict[str, str]] = {
    "skbrain_projector": {"group": "skbrain_ops_rw", "pw_env": "SKBRAIN_PG_PROJECTOR_PW"},
    "skbrain_reader": {"group": "skbrain_ops_ro", "pw_env": "SKBRAIN_PG_READER_PW"},
}

_REDACTED = "***REDACTED***"


# --------------------------------------------------------------------------- #
# Manifest                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Migration:
    """One forward migration and its optional verify script."""

    name: str
    verify: str | None = None
    path_override: Path | None = None

    @property
    def path(self) -> Path:
        return self.path_override or (DEPLOY_DIR / self.name)

    @property
    def verify_path(self) -> Path | None:
        return DEPLOY_DIR / self.verify if self.verify else None


def load_manifest(manifest: Path = MANIFEST) -> list[Migration]:
    """Parse ``migrations.txt`` into an ordered list of Migration objects.

    Format per line: ``<migration.sql>[ | <verify.sql>]``. Blank lines and
    lines starting with ``#`` are ignored. Order is preserved verbatim (it IS
    the apply order). Historical/superseded migrations are simply not listed.
    """
    migrations: list[Migration] = []
    if not manifest.exists():
        return migrations
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "|" in line:
            name, verify = (part.strip() for part in line.split("|", 1))
        else:
            name, verify = line.strip(), ""
        migrations.append(Migration(name=name, verify=verify or None))
    return migrations


def resolve_migration(script: str, manifest: Path = MANIFEST) -> Migration:
    """Resolve a user-supplied SCRIPT (a manifest name or a path) to a Migration.

    A bare manifest name matches the manifest (so the verify script is carried
    along). Anything that is an existing file is used AS GIVEN, directory and
    all; a path never gets silently re-resolved by basename.
    """
    entries = load_manifest(manifest)
    for m in entries:
        if m.name == script:
            return m
    base = os.path.basename(script)
    if base == script:  # bare name only; a real path must not hit a same-named manifest entry
        pass
    elif not Path(script).expanduser().is_file():
        for m in entries:
            if m.name == base:
                return m
    # An existing filesystem path is honoured as given (directory included).
    p = Path(script).expanduser()
    if p.is_file():
        return Migration(name=p.name, path_override=p.resolve())
    # Otherwise a bare name may still be a file next to the manifest.
    candidate = DEPLOY_DIR / base
    if not candidate.exists():
        raise FileNotFoundError(
            f"migration {script!r} is not in {manifest.name} and no file found at {p} "
            f"or {candidate}"
        )
    return Migration(name=base)


# --------------------------------------------------------------------------- #
# Transport                                                                    #
# --------------------------------------------------------------------------- #


_KV_PASSWORD = re.compile(r"(?i)(\bpassword\s*=\s*)(?:'(?:[^'\\]|\\.)*'|\S+)")


def split_dsn(dsn: str) -> tuple[str, str | None]:
    """Return ``(dsn_without_password, password_or_None)`` for a libpq DSN.

    Handles URL form (``postgresql://user:pw@host/db``, ``?password=``) and
    keyword form (``host=.. password=..``). The password is meant for PGPASSWORD.
    """
    if "://" in dsn:
        parts = urlsplit(dsn)
        pw = unquote(parts.password) if parts.password is not None else None
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, hostpart = netloc.rsplit("@", 1)
            netloc = (userinfo.split(":", 1)[0] + "@" + hostpart) if userinfo else hostpart
        query = parts.query
        if query:
            keep = []
            for kv in query.split("&"):
                if kv.lower().startswith("password="):
                    pw = pw or unquote(kv.split("=", 1)[1])
                else:
                    keep.append(kv)
            query = "&".join(keep)
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment)), pw
    m = _KV_PASSWORD.search(dsn)
    if not m:
        return dsn, None
    raw = m.group(0).split("=", 1)[1].strip()
    if raw.startswith("'"):
        raw = re.sub(r"\\(.)", r"\1", raw[1:-1])
    return _KV_PASSWORD.sub("", dsn).strip(), raw


def redact_dsn(dsn: str) -> str:
    """Display form of a DSN: user@host:port/db with any password shown as ***."""
    if "://" in dsn:
        parts = urlsplit(dsn)
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, hostpart = netloc.rsplit("@", 1)
            if ":" in userinfo:
                netloc = userinfo.split(":", 1)[0] + ":***@" + hostpart
        query = "&".join(
            "password=***" if kv.lower().startswith("password=") else kv
            for kv in parts.query.split("&")
            if kv
        )
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    return _KV_PASSWORD.sub(lambda m: re.sub(r"=.*", "=***", m.group(1)), dsn)


def scrub(text: str, dsn: str | None = None, secret: str | None = None) -> str:
    """Remove DSN passwords (and the password of ``dsn``) from arbitrary text."""
    text = re.sub(r"(://[^:/@\s]*:)[^@\s]*@", r"\1***@", text)
    text = _KV_PASSWORD.sub(lambda m: re.sub(r"=.*", "=***", m.group(1)), text)
    if secret:
        text = text.replace(secret, "***")
    if dsn:
        pw = split_dsn(dsn)[1]
        if pw:
            text = text.replace(pw, "***").replace(quote(pw, safe=""), "***")
    return text


@dataclass
class Target:
    """Where migrations apply. Exactly one transport is used.

    docker (default): ``docker exec -i <container> psql -U <user> -d <db>`` --
        the exact invocation from the README apply step.
    dsn: a libpq connection string used with a local/remote ``psql``.
    """

    container: str = "skmem-pg"
    dsn: str | None = None
    db: str = "skmemory"
    user: str = "postgres"

    @property
    def uses_dsn(self) -> bool:
        return bool(self.dsn)

    @property
    def clean_dsn(self) -> str:
        """The DSN with its password removed (safe for argv)."""
        return split_dsn(self.dsn or "")[0]

    @property
    def display_dsn(self) -> str:
        return redact_dsn(self.dsn or "")

    def env(self) -> dict[str, str]:
        """Extra environment for the child: PGPASSWORD, never argv."""
        pw = split_dsn(self.dsn)[1] if self.dsn else None
        return {"PGPASSWORD": pw} if pw else {}

    def psql_argv(self) -> list[str]:
        """psql argv, reading SQL from STDIN, ON_ERROR_STOP set."""
        if self.uses_dsn:
            return ["psql", self.clean_dsn, "-v", "ON_ERROR_STOP=1"]
        return [
            "docker",
            "exec",
            "-i",
            self.container,
            "psql",
            "-U",
            self.user,
            "-d",
            self.db,
            "-v",
            "ON_ERROR_STOP=1",
        ]

    def pg_dump_argv(self) -> list[str]:
        """pg_dump -Fc argv (custom format, for pre-apply insurance)."""
        if self.uses_dsn:
            return ["pg_dump", "-Fc", "-d", self.clean_dsn]
        return ["docker", "exec", self.container, "pg_dump", "-U", self.user, "-Fc", self.db]


# --------------------------------------------------------------------------- #
# Plans                                                                        #
# --------------------------------------------------------------------------- #


@dataclass
class Step:
    """One executable step in a plan.

    argv          : the command to run.
    stdin_path    : a file whose bytes are piped to argv's stdin (SQL apply).
    stdin_text    : literal text piped to stdin (role DDL; may hold a secret).
    stdout_path   : capture stdout to this host file (pg_dump).
    label         : human description for logs / dry-run.
    secret        : True if stdin_text carries a password (redact in output).
    fatal         : True if a non-zero exit aborts the plan (verify is not fatal).
    """

    argv: list[str]
    label: str
    stdin_path: Path | None = None
    stdin_text: str | None = None
    stdout_path: Path | None = None
    secret: bool = False
    fatal: bool = True
    env: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        cmd = scrub(" ".join(self.argv))
        if self.env:
            cmd = "PGPASSWORD=*** " + cmd
        redir = ""
        if self.stdin_path:
            redir = f"  < {self.stdin_path}"
        elif self.stdin_text is not None:
            redir = "  < (stdin: DDL, password redacted)" if self.secret else "  < (stdin)"
        if self.stdout_path:
            redir += f"  > {self.stdout_path}"
        return f"{self.label}: {cmd}{redir}"


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)

    def describe(self) -> str:
        if not self.steps:
            return "(no steps: nothing to do)"
        return "\n".join(f"  {i + 1}. {s.describe()}" for i, s in enumerate(self.steps))


def build_migrate_plan(
    migrations: list[Migration],
    target: Target,
    *,
    pre_dump: bool = True,
    verify: bool = True,
    dump_dir: Path | None = None,
) -> Plan:
    """Build the ordered plan to apply ``migrations`` to ``target``.

    Order per migration: [pre_dump] -> apply -> [verify]. A single pre-dump is
    taken once, before the first apply (it snapshots the whole DB). Every apply
    runs the migration file on psql stdin with ON_ERROR_STOP; verify runs the
    associated verify script (non-fatal). Idempotent: re-running the same plan
    re-applies guarded, no-op SQL.
    """
    plan = Plan()
    if not migrations:
        return plan

    if pre_dump:
        dump_dir = dump_dir or (Path.home() / "skmem-backups")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dump_file = dump_dir / f"skmemory-pre-migrate-{stamp}.dump"
        plan.steps.append(
            Step(
                argv=target.pg_dump_argv(),
                label="pre-dump (rollback insurance)",
                stdout_path=dump_file,
                env=target.env(),
            )
        )

    for m in migrations:
        argv = target.psql_argv()
        if target.uses_dsn:
            # DSN transport reads the file directly (-f); no stdin.
            argv = argv + ["-f", str(m.path)]
            plan.steps.append(Step(argv=argv, label=f"apply {m.name}", env=target.env()))
        else:
            plan.steps.append(
                Step(argv=argv, label=f"apply {m.name}", stdin_path=m.path, env=target.env())
            )
        if verify and m.verify_path and m.verify_path.exists():
            vargv = target.psql_argv()
            if target.uses_dsn:
                vargv = vargv + ["-f", str(m.verify_path)]
                plan.steps.append(
                    Step(argv=vargv, label=f"verify {m.verify}", fatal=False, env=target.env())
                )
            else:
                plan.steps.append(
                    Step(
                        argv=vargv,
                        label=f"verify {m.verify}",
                        stdin_path=m.verify_path,
                        fatal=False,
                        env=target.env(),
                    )
                )
    return plan


def _role_ddl(login: str, group: str, password: str) -> str:
    """Idempotent DDL binding a LOGIN role into a group role.

    CREATE the login role if missing else ALTER it (both set LOGIN + password),
    then GRANT the group membership (GRANT is idempotent). Password is embedded
    as a single-quoted literal with quotes doubled; the whole string is piped on
    stdin only, never argv. pg_roles guard makes the create branch a no-op on
    re-run.
    """
    esc = password.replace("'", "''")
    return (
        f"DO $$\nBEGIN\n"
        f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{login}') THEN\n"
        f"    CREATE ROLE {login} LOGIN PASSWORD '{esc}';\n"
        f"  ELSE\n"
        f"    ALTER ROLE {login} WITH LOGIN PASSWORD '{esc}';\n"
        f"  END IF;\n"
        f"END\n$$;\n"
        f"GRANT {group} TO {login};\n"
    )


def build_roles_plan(
    target: Target,
    *,
    bindings: dict[str, dict[str, str]] | None = None,
    env: dict[str, str] | None = None,
    skip_missing: bool = False,
) -> Plan:
    """Build the plan that creates/ALTERs LOGIN roles bound into the ops groups.

    Closes the G2 credential gap: 03-ops-namespace.sql creates only the NOLOGIN
    group roles skbrain_ops_rw/ro, so the projector cannot connect. Each login
    role's password is read from ``env`` (default os.environ), populated from
    skvault. A missing password raises unless ``skip_missing`` is set (then that
    role is skipped). Passwords are carried on stdin only and marked secret.
    """
    bindings = bindings or ROLE_BINDINGS
    env = env if env is not None else dict(os.environ)
    plan = Plan()
    for login, spec in bindings.items():
        pw = env.get(spec["pw_env"])
        if not pw:
            if skip_missing:
                continue
            raise KeyError(
                f"password for role {login!r} not set: export {spec['pw_env']} "
                f"(sourced from skvault via ~/.config/environment.d/skbrain.conf)"
            )
        plan.steps.append(
            Step(
                argv=target.psql_argv(),
                label=f"bind login role {login} -> {spec['group']}",
                stdin_text=_role_ddl(login, spec["group"], pw),
                secret=True,
                env=target.env(),
            )
        )
    return plan


# --------------------------------------------------------------------------- #
# Execution                                                                    #
# --------------------------------------------------------------------------- #


class MigrationError(RuntimeError):
    pass


def run_plan(plan: Plan, *, runner=subprocess.run, echo=print) -> list[dict]:
    """Execute a plan step by step. Returns per-step result dicts.

    ``runner`` is injectable (subprocess.run by default) so execution ordering
    and stdin/redirection wiring are testable without psql or docker. A fatal
    step that exits non-zero raises MigrationError; a non-fatal step (verify)
    only logs. Secret stdin is never echoed.
    """
    results: list[dict] = []
    for step in plan.steps:
        echo(f"-> {step.describe()}")
        stdin_data: bytes | None = None
        if step.stdin_path is not None:
            stdin_data = step.stdin_path.read_bytes()
        elif step.stdin_text is not None:
            stdin_data = step.stdin_text.encode("utf-8")

        if step.stdout_path is not None:
            step.stdout_path.parent.mkdir(parents=True, exist_ok=True)

        kwargs: dict = {"input": stdin_data, "capture_output": True}
        if step.env:  # only then, so simple runner fakes keep working
            kwargs["env"] = {**os.environ, **step.env}
        proc = runner(step.argv, **kwargs)
        rc = getattr(proc, "returncode", 0)
        stdout = getattr(proc, "stdout", b"") or b""
        stderr = getattr(proc, "stderr", b"") or b""
        if isinstance(stdout, str):
            stdout = stdout.encode()
        if isinstance(stderr, str):
            stderr = stderr.encode()

        if step.stdout_path is not None and rc == 0:
            step.stdout_path.write_bytes(stdout)

        result = {
            "label": step.label,
            "returncode": rc,
            "stdout": "" if step.stdout_path is not None else stdout.decode("utf-8", "replace"),
            "stderr": scrub(
                _redact(stderr.decode("utf-8", "replace"))
                if step.secret
                else stderr.decode("utf-8", "replace"),
                secret=step.env.get("PGPASSWORD"),
            ),
        }
        results.append(result)

        if rc != 0:
            if step.fatal:
                raise MigrationError(
                    f"step failed ({step.label}, rc={rc}): {result['stderr'].strip()}"
                )
            echo(f"   (non-fatal) {step.label} returned rc={rc}")
        else:
            if result["stdout"].strip():
                echo(result["stdout"].rstrip())
    return results


def _redact(text: str) -> str:
    # Best-effort: role DDL failures should not leak the literal password.
    return re.sub(r"PASSWORD '(?:[^']|'')*'", f"PASSWORD '{_REDACTED}'", text)


def prepare_target(
    target: Target,
    *,
    need_dump: bool,
    which=shutil.which,
    container_running=None,
    echo=print,
) -> Target:
    """Make a DSN target runnable, falling back to docker exec when it must.

    With a DSN, host ``psql`` (and ``pg_dump`` when a pre-dump is wanted) are
    required. If they are missing and the container exists, switch to docker
    exec mode with a clear message; otherwise raise MigrationError naming what
    is missing. Non-DSN targets are returned unchanged.
    """
    if not target.uses_dsn:
        return target
    missing = [b for b in (["psql"] + (["pg_dump"] if need_dump else [])) if not which(b)]
    if not missing:
        return target
    if container_running is None:

        def container_running(name: str) -> bool:
            if not which("docker"):
                return False
            r = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", name],
                capture_output=True,
                text=True,
            )
            return r.returncode == 0 and r.stdout.strip() == "true"

    if container_running(target.container):
        echo(
            f"note: {', '.join(missing)} not found on this host; using docker exec "
            f"against container {target.container!r} instead of the DSN."
        )
        db, user = target.db, target.user
        parts = urlsplit(target.clean_dsn) if "://" in (target.dsn or "") else None
        if parts is not None:
            db = parts.path.lstrip("/") or db
            user = parts.username or user
        return Target(container=target.container, dsn=None, db=db, user=user)
    raise MigrationError(
        f"{', '.join(missing)} not found on this host and docker container "
        f"{target.container!r} is not running; install the postgres client tools "
        f"or run with --container."
    )
