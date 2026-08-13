-- =====================================================================
-- Furnace Process Monitor - SQL Server schema
--
-- This file is a printable copy of what db.py creates automatically on
-- first connect. Hand it to a DBA (and set SQL_AUTO_CREATE_SCHEMA=0) if
-- the application login should not have DDL rights.
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
    HeatId          VARCHAR(64)    NOT NULL CONSTRAINT PK_Heats PRIMARY KEY,
    HeatNumber      INT            NULL,
    StartTime       DATETIME2(3)   NULL,
    EndTime         DATETIME2(3)   NULL,
    DurationMinutes DECIMAL(10, 2) NULL,
    TiltCount       INT            NOT NULL CONSTRAINT DF_Heats_TiltCount DEFAULT (0),
    Status          VARCHAR(20)    NOT NULL CONSTRAINT DF_Heats_Status DEFAULT ('IN_PROGRESS'),
    CsvFile         VARCHAR(255)   NULL,
    SourceHost      VARCHAR(128)   NULL,
    CreatedAt       DATETIME2(3)   NOT NULL CONSTRAINT DF_Heats_CreatedAt DEFAULT (SYSDATETIME()),
    UpdatedAt       DATETIME2(3)   NOT NULL CONSTRAINT DF_Heats_UpdatedAt DEFAULT (SYSDATETIME())
);
GO

IF OBJECT_ID('dbo.HeatEvents', 'U') IS NULL
CREATE TABLE [dbo].[HeatEvents] (
    EventId         BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_HeatEvents PRIMARY KEY,
    EventKey        CHAR(36)       NOT NULL,
    HeatId          VARCHAR(64)    NOT NULL,
    HeatNumber      INT            NULL,
    EventName       VARCHAR(64)    NOT NULL,
    StartTime       DATETIME2(3)   NULL,
    EndTime         DATETIME2(3)   NULL,
    DurationMinutes DECIMAL(10, 2) NULL,
    TiltNumber      INT            NULL,
    Note            VARCHAR(255)   NULL,
    SourceHost      VARCHAR(128)   NULL,
    CreatedAt       DATETIME2(3)   NOT NULL CONSTRAINT DF_HeatEvents_CreatedAt DEFAULT (SYSDATETIME())
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'UX_HeatEvents_EventKey'
                 AND object_id = OBJECT_ID('dbo.HeatEvents'))
    CREATE UNIQUE INDEX UX_HeatEvents_EventKey ON [dbo].[HeatEvents] (EventKey);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_HeatEvents_HeatId'
                 AND object_id = OBJECT_ID('dbo.HeatEvents'))
    CREATE INDEX IX_HeatEvents_HeatId ON [dbo].[HeatEvents] (HeatId)
        INCLUDE (EventName, StartTime, EndTime, DurationMinutes, TiltNumber);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_HeatEvents_Name_Start'
                 AND object_id = OBJECT_ID('dbo.HeatEvents'))
    CREATE INDEX IX_HeatEvents_Name_Start ON [dbo].[HeatEvents] (EventName, StartTime);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_Heats_StartTime'
                 AND object_id = OBJECT_ID('dbo.Heats'))
    CREATE INDEX IX_Heats_StartTime ON [dbo].[Heats] (StartTime) INCLUDE (Status, TiltCount);
GO

CREATE OR ALTER VIEW [dbo].[vw_HeatAnalysis] AS
WITH stages AS (
    SELECT
        HeatId,
        MIN(CASE WHEN EventName = 'scrap_charging'  THEN StartTime END) AS ScrapStart,
        MAX(CASE WHEN EventName = 'scrap_charging'  THEN EndTime   END) AS ScrapEnd,
        SUM(CASE WHEN EventName = 'scrap_charging'  THEN DurationMinutes END) AS ScrapMinutes,
        MIN(CASE WHEN EventName = 'sponge_charging' THEN StartTime END) AS SpongeStart,
        MAX(CASE WHEN EventName = 'sponge_charging' THEN EndTime   END) AS SpongeEnd,
        SUM(CASE WHEN EventName = 'sponge_charging' THEN DurationMinutes END) AS SpongeMinutes,
        MIN(CASE WHEN EventName = 'tapping'         THEN StartTime END) AS TappingStart,
        MAX(CASE WHEN EventName = 'tapping'         THEN EndTime   END) AS TappingEnd,
        SUM(CASE WHEN EventName = 'tapping'         THEN DurationMinutes END) AS TappingMinutes,
        SUM(CASE WHEN EventName = 'tilt' THEN 1 ELSE 0 END)                  AS TiltEvents,
        SUM(CASE WHEN EventName = 'tilt' THEN DurationMinutes ELSE 0 END)    AS TiltMinutes,
        SUM(CASE WHEN EventName = 'tilt_ignored_cooldown' THEN 1 ELSE 0 END) AS TiltsSuppressed,
        MAX(CASE WHEN EventName LIKE '%[_]unknown' THEN 1 ELSE 0 END)        AS HasUnknownStage
    FROM [dbo].[HeatEvents]
    GROUP BY HeatId
)
SELECT
    h.HeatId,
    h.HeatNumber,
    h.Status,
    h.StartTime,
    h.EndTime,
    h.DurationMinutes                             AS HeatMinutes,
    CAST(h.StartTime AS DATE)                     AS HeatDate,
    DATEPART(HOUR, h.StartTime)                   AS HeatStartHour,
    DATENAME(WEEKDAY, h.StartTime)                AS HeatWeekday,
    s.ScrapStart,  s.ScrapEnd,  s.ScrapMinutes,
    s.SpongeStart, s.SpongeEnd, s.SpongeMinutes,
    s.TappingStart, s.TappingEnd, s.TappingMinutes,
    h.TiltCount,
    ISNULL(s.TiltMinutes, 0)                      AS TiltMinutes,
    ISNULL(s.TiltsSuppressed, 0)                  AS TiltsSuppressed,
    -- Anything not accounted for by the three stages (gaps, idle time
    -- inside the heat) - useful for spotting unexplained losses.
    CASE WHEN h.DurationMinutes IS NOT NULL
         THEN h.DurationMinutes
              - ISNULL(s.ScrapMinutes, 0)
              - ISNULL(s.SpongeMinutes, 0)
              - ISNULL(s.TappingMinutes, 0)
    END                                           AS UnaccountedMinutes,
    -- Flags against the plant's own reference ranges.
    CASE WHEN s.ScrapMinutes   BETWEEN  20 AND  30 THEN 1 ELSE 0 END AS ScrapWithinExpected,
    CASE WHEN s.SpongeMinutes  BETWEEN  60 AND 150 THEN 1 ELSE 0 END AS SpongeWithinExpected,
    CASE WHEN s.TappingMinutes BETWEEN  10 AND  20 THEN 1 ELSE 0 END AS TappingWithinExpected,
    -- 1 when one or more stages were never observed (app started
    -- mid-heat), so these rows can be excluded from averages.
    ISNULL(s.HasUnknownStage, 0)                  AS HasUnknownStage,
    h.CsvFile,
    h.SourceHost,
    h.UpdatedAt
FROM [dbo].[Heats] h
LEFT JOIN stages s ON s.HeatId = h.HeatId;
GO

CREATE OR ALTER VIEW [dbo].[vw_HeatDaily] AS
SELECT
    HeatDate,
    COUNT(*)                        AS HeatsCompleted,
    CAST(AVG(HeatMinutes)    AS DECIMAL(10,2)) AS AvgHeatMinutes,
    CAST(MIN(HeatMinutes)    AS DECIMAL(10,2)) AS MinHeatMinutes,
    CAST(MAX(HeatMinutes)    AS DECIMAL(10,2)) AS MaxHeatMinutes,
    CAST(AVG(ScrapMinutes)   AS DECIMAL(10,2)) AS AvgScrapMinutes,
    CAST(AVG(SpongeMinutes)  AS DECIMAL(10,2)) AS AvgSpongeMinutes,
    CAST(AVG(TappingMinutes) AS DECIMAL(10,2)) AS AvgTappingMinutes,
    SUM(TiltCount)                  AS TotalTilts,
    CAST(AVG(CAST(TiltCount AS DECIMAL(10,2))) AS DECIMAL(10,2)) AS AvgTiltsPerHeat,
    SUM(TiltsSuppressed)            AS TotalTiltsSuppressed
FROM [dbo].[vw_HeatAnalysis]
WHERE Status = 'COMPLETE' AND HasUnknownStage = 0
GROUP BY HeatDate;
GO
