# bronze_json_schema.py — once-off JSON structure explorer
import json, gzip
from sqlalchemy import text as sql_text
from db_init import engine  # uses same engine as bronze

def _summarize_node(node, max_scan=200):
    if isinstance(node, dict):
        return {
            "type": "object",
            "subheaders": sorted(node.keys())
        }

    if isinstance(node, list):
        keys = set()
        scanned = 0
        for it in node[:max_scan]:
            scanned += 1
            if isinstance(it, dict):
                keys |= set(it.keys())
        return {
            "type": "array<object>" if keys else "array",
            "subheaders": sorted(keys),
            "count_scanned": scanned
        }

    return {"type": type(node).__name__}

def extract_json_schema(session_id: int, max_scan: int = 200):
    """Extract and print JSON headers + subheaders from bronze.raw_result."""
    with engine.begin() as conn:
        row = conn.execute(sql_text("""
            SELECT payload_json, payload_gzip
            FROM bronze.raw_result
            WHERE session_id=:sid
            ORDER BY created_at DESC
            LIMIT 1
        """), {"sid": session_id}).first()

        if not row:
            print(f"No raw_result found for session {session_id}")
            return

        pj, gz = row[0], row[1]
        if pj is not None:
            payload = pj if isinstance(pj, dict) else json.loads(pj)
        elif gz is not None:
            payload = json.loads(gzip.decompress(gz).decode("utf-8"))
        else:
            print("Empty raw_result")
            return

    if not isinstance(payload, dict):
        print("Payload is not a dict at top-level")
        return

    result = {}
    for header, node in payload.items():
        # dict<string, array<object>> pattern (e.g., player_positions)
        if isinstance(node, dict):
            all_keys = set()
            scanned = 0
            is_dict_of_array = False
            for v in node.values():
                if isinstance(v, list):
                    is_dict_of_array = True
                    for it in v[:max_scan]:
                        scanned += 1
                        if isinstance(it, dict):
                            all_keys |= set(it.keys())
            if is_dict_of_array:
                result[header] = {
                    "type": "dict<string,array<object>>",
                    "subheaders": sorted(all_keys),
                    "count_scanned": scanned
                }
            else:
                result[header] = _summarize_node(node, max_scan=max_scan)
        else:
            result[header] = _summarize_node(node, max_scan=max_scan)

    # Print neatly
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python bronze_json_schema.py <session_id>")
        sys.exit(1)
    sid = int(sys.argv[1])
    extract_json_schema(sid)
