def make_sql(cur):
    # If silver.vw_point_silver exists, create a simple alias view silver.vw_point
    # so downstream code can select from a stable name.
    return """
    DO $$
    BEGIN
      IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='silver' AND table_name='vw_point_silver'
      ) THEN
        EXECUTE 'CREATE OR REPLACE VIEW silver.vw_point AS SELECT * FROM silver.vw_point_silver';
      END IF;
    END $$;
    """
