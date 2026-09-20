"""PostGIS generated indexes; SQLite retains its portable JSON representation."""
from django.db import migrations


def create_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    statements = [
        "CREATE EXTENSION IF NOT EXISTS postgis",
        """ALTER TABLE map_api_spatialattachment ADD COLUMN geographic_footprint geometry(Polygon,4326)
            GENERATED ALWAYS AS (CASE WHEN bbox IS NOT NULL AND bbox ?& ARRAY['min_lng','min_lat','max_lng','max_lat']
            THEN ST_MakeEnvelope((bbox->>'min_lng')::double precision, (bbox->>'min_lat')::double precision,
                                (bbox->>'max_lng')::double precision, (bbox->>'max_lat')::double precision,4326)
            ELSE NULL END) STORED""",
        "CREATE INDEX v3_attachment_footprint ON map_api_spatialattachment USING GIST (geographic_footprint)",
        """ALTER TABLE map_api_spatialobservation ADD COLUMN pixel_footprint geometry(Polygon,0)
            GENERATED ALWAYS AS (CASE WHEN jsonb_array_length("window")=4
            THEN ST_MakeEnvelope(("window"->>0)::double precision, ("window"->>1)::double precision,
                ("window"->>0)::double precision+("window"->>2)::double precision,
                ("window"->>1)::double precision+("window"->>3)::double precision,0)
            ELSE NULL END) STORED""",
        "CREATE INDEX v3_observation_footprint ON map_api_spatialobservation USING GIST (pixel_footprint)",
    ]
    for statement in statements:
        schema_editor.execute(statement)


def drop_indexes(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("ALTER TABLE map_api_spatialobservation DROP COLUMN pixel_footprint")
        schema_editor.execute("ALTER TABLE map_api_spatialattachment DROP COLUMN geographic_footprint")


class Migration(migrations.Migration):
    dependencies = [("map_api", "0026_conversation_workbench")]
    operations = [migrations.RunPython(create_indexes, drop_indexes)]
