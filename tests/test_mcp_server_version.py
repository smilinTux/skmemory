"""serverInfo carries skmemory's own version, not the mcp library's."""

from skmemory import __version__
from skmemory.mcp_server import server


def test_server_reports_skmemory_version():
    opts = server.create_initialization_options()
    assert opts.server_name == "skmemory"
    assert opts.server_version == __version__
