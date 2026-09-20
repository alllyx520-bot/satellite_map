"""Owner-scoped spatial lookup, accelerated by PostGIS when configured."""
from django.db import connection
from .assets import observation_payload
from .spatial_tools import get_attachment


def search(args, context):
    attachment = get_attachment(context, args["attachment_id"])
    rows = attachment.observations.filter(conversation=context["conversation"]).order_by("created_at")
    if args.get("label"):
        rows = rows.filter(label__icontains=args["label"])
    window = args.get("window")
    if window:
        x, y, width, height = window
        if min(x, y) < 0 or min(width, height) <= 0 or x+width > attachment.width or y+height > attachment.height:
            raise ValueError("检索窗口超出影像范围")
        if connection.vendor == "postgresql":
            rows = rows.extra(where=["pixel_footprint && ST_MakeEnvelope(%s,%s,%s,%s,0)"],
                              params=[x, y, x+width, y+height])
        else:
            from shapely.geometry import box
            query = box(x, y, x+width, y+height)
            rows = [row for row in rows if len(row.window) == 4 and query.intersects(
                box(row.window[0], row.window[1], row.window[0]+row.window[2], row.window[1]+row.window[3]))]
    limit = args.get("limit", 20)
    selected = list(rows[:limit+1])
    return {"items": [observation_payload(row) for row in selected[:limit]], "truncated": len(selected)>limit,
            "coordinate_space": "image_pixels", "index": "postgis" if connection.vendor == "postgresql" else "scoped_json_scan"}
