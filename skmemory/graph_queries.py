"""
Cypher query templates for SKGraph (FalkorDB) graph operations.

All graph queries are defined here as constants for maintainability
and testability. The SKGraphBackend imports from this module so
query strings never live inline in business logic.

FalkorDB uses a Cypher dialect compatible with RedisGraph and Neo4j.
MERGE is idempotent: safe to call repeatedly, creates if not present.
"""

# ═══════════════════════════════════════════════════════════
# Node creation / upsert
# ═══════════════════════════════════════════════════════════

#: Create or update a Memory node with key properties.
UPSERT_MEMORY = """
MERGE (m:Memory {id: $id})
SET m.title = $title,
    m.layer = $layer,
    m.source = $source,
    m.source_ref = $source_ref,
    m.intensity = $intensity,
    m.valence = $valence,
    m.created_at = $created_at,
    m.updated_at = $updated_at
"""

#: Create or update a Tag node.
UPSERT_TAG = """
MERGE (t:Tag {name: $name})
SET t.name = $name
"""

#: Create or update a Source node (mcp, cli, seed, session, etc.).
UPSERT_SOURCE = """
MERGE (s:Source {name: $name})
SET s.name = $name
"""

#: Create or update an AI node for seed creators.
UPSERT_AI = """
MERGE (a:AI {name: $name})
SET a.name = $name
"""

# ═══════════════════════════════════════════════════════════
# Relationship creation
# ═══════════════════════════════════════════════════════════

#: Connect Memory to Tag with a TAGGED edge.
CREATE_TAGGED = """
MATCH (m:Memory {id: $mem_id})
MERGE (t:Tag {name: $tag})
MERGE (m)-[:TAGGED]->(t)
"""

#: Connect Memory to Source with a FROM_SOURCE edge.
CREATE_FROM_SOURCE = """
MATCH (m:Memory {id: $mem_id})
MERGE (s:Source {name: $source})
MERGE (m)-[:FROM_SOURCE]->(s)
"""

#: Connect two memories with a directional RELATED_TO edge.
CREATE_RELATED_TO = """
MATCH (a:Memory {id: $a_id})
MERGE (b:Memory {id: $b_id})
MERGE (a)-[:RELATED_TO]->(b)
"""

#: Connect a promoted memory back to its origin with a PROMOTED_FROM edge.
CREATE_PROMOTED_FROM = """
MATCH (child:Memory {id: $child_id})
MERGE (parent:Memory {id: $parent_id})
MERGE (child)-[:PROMOTED_FROM]->(parent)
"""

#: Connect memories from the same source that share 2+ tags with RELATED_TO.
#: Used by index_memory() to auto-wire shared-tag neighbours.
CREATE_SHARED_TAG_RELATED = """
MATCH (a:Memory {id: $a_id})-[:TAGGED]->(t:Tag)<-[:TAGGED]-(b:Memory)
WHERE b.id <> $a_id
WITH a, b, count(DISTINCT t) AS overlap
WHERE overlap >= 2
MERGE (a)-[:RELATED_TO]->(b)
"""

#: Connect two sequential memories from the same source with PRECEDED_BY.
CREATE_PRECEDED_BY = """
MATCH (later:Memory {id: $later_id})
MATCH (earlier:Memory {id: $earlier_id})
MERGE (later)-[:PRECEDED_BY]->(earlier)
"""

#: Connect AI creator to its planted seed memory.
CREATE_PLANTED = """
MATCH (m:Memory {id: $mem_id})
MERGE (a:AI {name: $creator})
MERGE (a)-[:PLANTED]->(m)
"""

# ═══════════════════════════════════════════════════════════
# Traversal queries
# ═══════════════════════════════════════════════════════════

#: Traverse all edges up to N hops from a starting memory.
#: Depth is interpolated at call time (not a parameter) because
#: FalkorDB does not support parameterised variable-length path lengths.
TRAVERSE_RELATED = """
MATCH (start:Memory {{id: $id}})
MATCH path = (start)-[*1..{depth}]-(related:Memory)
WHERE related.id <> $id
RETURN DISTINCT related.id AS id,
       related.title AS title,
       related.layer AS layer,
       related.intensity AS intensity,
       length(path) AS distance
ORDER BY distance ASC, related.intensity DESC
LIMIT 20
"""

#: Walk PROMOTED_FROM chain upward to find ancestor memories.
TRAVERSE_LINEAGE = """
MATCH (start:Memory {id: $id})
MATCH path = (start)-[:PROMOTED_FROM*1..10]->(ancestor:Memory)
RETURN ancestor.id AS id,
       ancestor.title AS title,
       ancestor.layer AS layer,
       length(path) AS depth
ORDER BY depth ASC
"""

# ═══════════════════════════════════════════════════════════
# Cluster / community queries
# ═══════════════════════════════════════════════════════════

#: Find memories that are cluster hubs (many direct neighbours).
FIND_CLUSTER_HUBS = """
MATCH (m:Memory)-[r]-(connected:Memory)
WITH m, count(DISTINCT connected) AS connections
WHERE connections >= $min_connections
RETURN m.id AS id,
       m.title AS title,
       m.layer AS layer,
       connections
ORDER BY connections DESC
LIMIT 20
"""

#: Retrieve all memories reachable from a hub (for cluster membership).
GET_CLUSTER_MEMBERS = """
MATCH (hub:Memory {id: $hub_id})-[*1..2]-(member:Memory)
WHERE member.id <> $hub_id
RETURN DISTINCT member.id AS id,
       member.title AS title,
       member.layer AS layer,
       member.intensity AS intensity
"""

# ═══════════════════════════════════════════════════════════
# Search queries
# ═══════════════════════════════════════════════════════════

#: Full-text search across Memory titles using CONTAINS.
SEARCH_BY_TITLE = """
MATCH (m:Memory)
WHERE toLower(m.title) CONTAINS toLower($query)
RETURN m.id AS id,
       m.title AS title,
       m.layer AS layer,
       m.intensity AS intensity,
       m.created_at AS created_at
ORDER BY m.intensity DESC
LIMIT $limit
"""

#: Find memories that share any of the given tags (OR logic).
SEARCH_BY_TAGS = """
MATCH (m:Memory)-[:TAGGED]->(t:Tag)
WHERE t.name IN $tags
WITH m, collect(DISTINCT t.name) AS matched_tags
RETURN m.id AS id,
       m.title AS title,
       m.layer AS layer,
       m.intensity AS intensity,
       matched_tags,
       size(matched_tags) AS tag_overlap
ORDER BY tag_overlap DESC, m.intensity DESC
LIMIT $limit
"""

#: Find the most recent memory from the same source (for PRECEDED_BY wiring).
FIND_PREVIOUS_FROM_SOURCE = """
MATCH (m:Memory)-[:FROM_SOURCE]->(s:Source {name: $source})
WHERE m.id <> $exclude_id
RETURN m.id AS id, m.created_at AS created_at
ORDER BY m.created_at DESC
LIMIT 1
"""

# ═══════════════════════════════════════════════════════════
# Stats / health queries
# ═══════════════════════════════════════════════════════════

#: Count all nodes in the graph.
COUNT_NODES = "MATCH (n) RETURN count(n) AS nodes"

#: Count Memory nodes specifically.
COUNT_MEMORIES = "MATCH (m:Memory) RETURN count(m) AS memories"

#: Count all relationships/edges.
COUNT_EDGES = "MATCH ()-[r]->() RETURN count(r) AS edges"

#: Count Tag nodes and get their names for distribution.
TAG_DISTRIBUTION = """
MATCH (t:Tag)<-[:TAGGED]-(m:Memory)
RETURN t.name AS tag, count(DISTINCT m) AS memory_count
ORDER BY memory_count DESC
LIMIT 20
"""

#: Retrieve a single memory node by ID.
GET_MEMORY_BY_ID = """
MATCH (m:Memory {id: $id})
RETURN m.id AS id,
       m.title AS title,
       m.layer AS layer,
       m.source AS source,
       m.source_ref AS source_ref,
       m.intensity AS intensity,
       m.valence AS valence,
       m.created_at AS created_at,
       m.updated_at AS updated_at
"""

#: Delete a memory node and all attached edges.
DELETE_MEMORY = "MATCH (m:Memory {id: $id}) DETACH DELETE m"
