-- =====================================================================
-- Furnace Process Monitor - SQL Server schema
--
-- This file is a printable copy of what db.py creates automatically on
-- first connect. Hand it to a DBA (and set SQL_AUTO_CREATE_SCHEMA=0) if
-- the application login should not have DDL rights.
--
-- FurnaceId: every row is tagged with the FurnaceId of the deployment
-- that wrote it (config.FURNACE_ID, e.g. '5/1'). Heats and HeatEvents
-- are shared tables across furnaces - filter or GROUP BY FurnaceId to
-- keep each furnace's data segregated in analysis (vw_HeatDaily is now
-- grouped by FurnaceId + HeatDate; see sql/analysis_queries.sql for
-- filtered examples). The ALTER TABLE blocks below add the column to a
-- database that already has these tables from before FurnaceId existed
-- - safe to run against a fresh database too, since they no-op there.
--
-- Regenerate with:  python3 -c "import db; print(chr(10).join(s.strip() + chr(10) + 'GO' for s in db.schema_statements()))"
-- =====================================================================

USE [Furnace];
GO

IF SCHEMA_ID('dbo') IS NULL
    EXEC('CREATE SCHEMA [dbo]');
GO

IF OBJECT_ID('dbo.Heats', 'U') IS NULL
CREATE TABLE [dbo].[Heats] (
    heat_id          VARCHAR(64)   NOT NULL CONSTRAINT PK_Heats PRIMARY KEY,
    furnace          VARCHAR(50)   NULL,
    stand            VARCHAR(50)   NULL,
    heat_number      INT           NULL,
    heat_status      VARCHAR(20)   NULL CONSTRAINT DF_Heats_heat_status DEFAULT ('IN_PROGRESS'),
    heat_start_time  DATETIME      NULL,
    heat_end_time    DATETIME      NULL,
    heat_duration    VARCHAR(50)   NULL,
    scrap_duration   VARCHAR(50)   NULL,
    sponge_duration  VARCHAR(50)   NULL,
    tapping_duration VARCHAR(50)   NULL,
    heat_event_match VARCHAR(5)    NULL,
    position         VARCHAR(20)   NULL,
    operation_status VARCHAR(20)   NULL,
    timeline_json    VARCHAR(MAX)  NULL,
    detected_classes VARCHAR(MAX)  NULL,
    insertion_time   DATETIME      NULL CONSTRAINT DF_Heats_insertion_time DEFAULT (SYSDATETIME()),
    updated_time     DATETIME      NULL CONSTRAINT DF_Heats_updated_time DEFAULT (SYSDATETIME()),
    FurnaceId        VARCHAR(16)   NOT NULL CONSTRAINT DF_Heats_FurnaceId DEFAULT ('')
);
GO

IF OBJECT_ID('dbo.HeatEvents', 'U') IS NULL
CREATE TABLE [dbo].[HeatEvents] (
    id               INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_HeatEvents PRIMARY KEY,
    heat_id          VARCHAR(64)       NULL,
    event_id         VARCHAR(64)       NULL,
    cross_event_id   VARCHAR(64)       NULL,
    event_name       VARCHAR(100)      NULL,
    cross_event_name VARCHAR(100)      NULL,
    start_time       DATETIME          NULL,
    end_time         DATETIME          NULL,
    duration         VARCHAR(50)       NULL,
    status           VARCHAR(20)       NULL,
    confidence       VARCHAR(20)       NULL,
    sop_duration     VARCHAR(50)       NULL,
    alarms           VARCHAR(MAX)      NULL,
    original_frame   VARBINARY(MAX)    NULL,
    plotted_image    VARBINARY(MAX)    NULL,
    created_at       DATETIME          NULL CONSTRAINT DF_HeatEvents_created_at DEFAULT (SYSDATETIME()),
    updated_at       DATETIME          NULL CONSTRAINT DF_HeatEvents_updated_at DEFAULT (SYSDATETIME()),
    FurnaceId        VARCHAR(16)       NOT NULL CONSTRAINT DF_HeatEvents_FurnaceId DEFAULT ('')
);
GO

-- Migration: add FurnaceId to tables that already existed before this
-- column was introduced. No-ops if the column is already there.
IF OBJECT_ID('dbo.Heats', 'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.columns
                    WHERE object_id = OBJECT_ID('dbo.Heats') AND name = 'FurnaceId')
    ALTER TABLE [dbo].[Heats] ADD FurnaceId VARCHAR(16) NOT NULL CONSTRAINT DF_Heats_FurnaceId DEFAULT ('');
GO

IF OBJECT_ID('dbo.HeatEvents', 'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.columns
                    WHERE object_id = OBJECT_ID('dbo.HeatEvents') AND name = 'FurnaceId')
    ALTER TABLE [dbo].[HeatEvents] ADD FurnaceId VARCHAR(16) NOT NULL CONSTRAINT DF_HeatEvents_FurnaceId DEFAULT ('');
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'UX_HeatEvents_event_id'
                 AND object_id = OBJECT_ID('dbo.HeatEvents'))
    CREATE UNIQUE INDEX UX_HeatEvents_event_id ON [dbo].[HeatEvents] (event_id) WHERE event_id IS NOT NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_Heats_FurnaceId'
                 AND object_id = OBJECT_ID('dbo.Heats'))
    CREATE INDEX IX_Heats_FurnaceId ON [dbo].[Heats] (FurnaceId, heat_start_time);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_HeatEvents_FurnaceId'
                 AND object_id = OBJECT_ID('dbo.HeatEvents'))
    CREATE INDEX IX_HeatEvents_FurnaceId ON [dbo].[HeatEvents] (FurnaceId, heat_id);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_HeatEvents_heat_id'
                 AND object_id = OBJECT_ID('dbo.HeatEvents'))
    CREATE INDEX IX_HeatEvents_heat_id ON [dbo].[HeatEvents] (heat_id)
        INCLUDE (event_name, start_time, end_time, duration);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_HeatEvents_name_start'
                 AND object_id = OBJECT_ID('dbo.HeatEvents'))
    CREATE INDEX IX_HeatEvents_name_start ON [dbo].[HeatEvents] (event_name, start_time);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_Heats_start_time'
                 AND object_id = OBJECT_ID('dbo.Heats'))
    CREATE INDEX IX_Heats_start_time ON [dbo].[Heats] (heat_start_time);
GO

CREATE OR ALTER VIEW [dbo].[vw_HeatAnalysis] AS
WITH stages AS (
    SELECT
        heat_id,
        MIN(CASE WHEN event_name = 'scrap_charging'  THEN start_time END) AS ScrapStart,
        MAX(CASE WHEN event_name = 'scrap_charging'  THEN end_time   END) AS ScrapEnd,
        SUM(CASE WHEN event_name = 'scrap_charging'  THEN TRY_CAST(duration AS DECIMAL(10,2)) END) AS ScrapMinutes,
        MIN(CASE WHEN event_name = 'sponge_charging' THEN start_time END) AS SpongeStart,
        MAX(CASE WHEN event_name = 'sponge_charging' THEN end_time   END) AS SpongeEnd,
        SUM(CASE WHEN event_name = 'sponge_charging' THEN TRY_CAST(duration AS DECIMAL(10,2)) END) AS SpongeMinutes,
        MIN(CASE WHEN event_name = 'tapping'         THEN start_time END) AS TappingStart,
        MAX(CASE WHEN event_name = 'tapping'         THEN end_time   END) AS TappingEnd,
        SUM(CASE WHEN event_name = 'tapping'         THEN TRY_CAST(duration AS DECIMAL(10,2)) END) AS TappingMinutes,
        SUM(CASE WHEN event_name = 'tilt' THEN 1 ELSE 0 END)                                      AS TiltCount,
        SUM(CASE WHEN event_name = 'tilt' THEN TRY_CAST(duration AS DECIMAL(10,2)) ELSE 0 END)    AS TiltMinutes,
        SUM(CASE WHEN event_name = 'tilt_ignored_cooldown' THEN 1 ELSE 0 END)                     AS TiltsSuppressed,
        MAX(CASE WHEN event_name LIKE '%[_]unknown' THEN 1 ELSE 0 END)                            AS HasUnknownStage
    FROM [dbo].[HeatEvents]
    GROUP BY heat_id
)
SELECT
    h.heat_id                                     AS HeatId,
    h.FurnaceId                                   AS FurnaceId,
    h.heat_number                                 AS HeatNumber,
    ISNULL(h.heat_status, 'IN_PROGRESS')          AS Status,
    h.heat_start_time                             AS StartTime,
    h.heat_end_time                               AS EndTime,
    TRY_CAST(h.heat_duration AS DECIMAL(10,2))    AS HeatMinutes,
    CAST(h.heat_start_time AS DATE)               AS HeatDate,
    DATEPART(HOUR, h.heat_start_time)             AS HeatStartHour,
    DATENAME(WEEKDAY, h.heat_start_time)          AS HeatWeekday,
    s.ScrapStart, s.ScrapEnd,
    COALESCE(s.ScrapMinutes, TRY_CAST(h.scrap_duration AS DECIMAL(10,2)))       AS ScrapMinutes,
    s.SpongeStart, s.SpongeEnd,
    COALESCE(s.SpongeMinutes, TRY_CAST(h.sponge_duration AS DECIMAL(10,2)))     AS SpongeMinutes,
    s.TappingStart, s.TappingEnd,
    COALESCE(s.TappingMinutes, TRY_CAST(h.tapping_duration AS DECIMAL(10,2)))   AS TappingMinutes,
    ISNULL(s.TiltCount, 0)                        AS TiltCount,
    ISNULL(s.TiltMinutes, 0)                      AS TiltMinutes,
    ISNULL(s.TiltsSuppressed, 0)                  AS TiltsSuppressed,
    CASE WHEN TRY_CAST(h.heat_duration AS DECIMAL(10,2)) IS NOT NULL
         THEN TRY_CAST(h.heat_duration AS DECIMAL(10,2))
              - ISNULL(COALESCE(s.ScrapMinutes, TRY_CAST(h.scrap_duration AS DECIMAL(10,2))), 0)
              - ISNULL(COALESCE(s.SpongeMinutes, TRY_CAST(h.sponge_duration AS DECIMAL(10,2))), 0)
              - ISNULL(COALESCE(s.TappingMinutes, TRY_CAST(h.tapping_duration AS DECIMAL(10,2))), 0)
    END                                           AS UnaccountedMinutes,
    CASE WHEN COALESCE(s.ScrapMinutes, TRY_CAST(h.scrap_duration AS DECIMAL(10,2))) <= 45 THEN 1 ELSE 0 END AS ScrapWithinExpected,
    CASE WHEN COALESCE(s.SpongeMinutes, TRY_CAST(h.sponge_duration AS DECIMAL(10,2))) BETWEEN 90 AND 120 THEN 1 ELSE 0 END AS SpongeWithinExpected,
    CASE WHEN COALESCE(s.TappingMinutes, TRY_CAST(h.tapping_duration AS DECIMAL(10,2))) BETWEEN 10 AND 20 THEN 1 ELSE 0 END AS TappingWithinExpected,
    ISNULL(s.HasUnknownStage, 0)                  AS HasUnknownStage,
    CAST(NULL AS VARCHAR(255))                    AS CsvFile,
    CAST(NULL AS VARCHAR(128))                    AS SourceHost,
    ISNULL(h.updated_time, SYSDATETIME())         AS UpdatedAt
FROM [dbo].[Heats] h
LEFT JOIN stages s ON s.heat_id = h.heat_id;
GO

CREATE OR ALTER VIEW [dbo].[vw_HeatDaily] AS
SELECT
    FurnaceId,
    HeatDate,
    COUNT(*)                                  AS HeatsCompleted,
    CAST(AVG(HeatMinutes)    AS DECIMAL(10,2)) AS AvgHeatMinutes,
    CAST(MIN(HeatMinutes)    AS DECIMAL(10,2)) AS MinHeatMinutes,
    CAST(MAX(HeatMinutes)    AS DECIMAL(10,2)) AS MaxHeatMinutes,
    CAST(AVG(ScrapMinutes)   AS DECIMAL(10,2)) AS AvgScrapMinutes,
    CAST(AVG(SpongeMinutes)  AS DECIMAL(10,2)) AS AvgSpongeMinutes,
    CAST(AVG(TappingMinutes) AS DECIMAL(10,2)) AS AvgTappingMinutes,
    SUM(TiltCount)                            AS TotalTilts,
    CAST(AVG(CAST(TiltCount AS DECIMAL(10,2))) AS DECIMAL(10,2)) AS AvgTiltsPerHeat,
    SUM(TiltsSuppressed)                      AS TotalTiltsSuppressed
FROM [dbo].[vw_HeatAnalysis]
WHERE Status = 'COMPLETE' AND HasUnknownStage = 0
GROUP BY FurnaceId, HeatDate;
GO
