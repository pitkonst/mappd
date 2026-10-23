-- Runs once, on an empty data volume. The app database `mappd` is created by POSTGRES_DB.
-- DBOS keeps its system tables in its own database; back up and restore both together.
-- Created databases are owned by POSTGRES_USER, whatever credential .env sets.
CREATE DATABASE mappd_dbos;
CREATE DATABASE mlflow;
