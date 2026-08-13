-- =====================================================================
-- Furnace Process Monitor - starter analysis queries
--
-- Everything below runs against dbo.vw_HeatAnalysis, which already joins
-- dbo.Heats to dbo.HeatEvents and pivots the three stages into columns.
-- Point Power BI / Excel at that view (or dbo.vw_HeatDaily for a
-- pre-aggregated daily rollup) rather than at the raw tables.
--
-- Note on filtering: rows with HasUnknownStage = 1 are heats the app
-- picked up mid-cycle after a restart, so one or more stage durations are
-- genuinely unknown rather than zero. Exclude them from averages.
--
-- Note on furnaces: Heats and HeatEvents are shared tables across every
-- furnace that points at this database - each row carries a FurnaceId
-- ('4/2', '4/1', ...). Every query below can be scoped to one furnace by
-- adding "WHERE FurnaceId = '4/2'" (query 1 shows the pattern); drop that
-- filter to see all furnaces together, as in query 9.
-- =====================================================================

USE [Furnace];
GO

-- ---------------------------------------------------------------------
-- 1. The last 50 heats for one furnace, stage by stage
-- ---------------------------------------------------------------------
SELECT TOP 50
    HeatId, FurnaceId, Status, StartTime, EndTime, HeatMinutes,
    ScrapMinutes, SpongeMinutes, TappingMinutes,
    TiltCount, TiltMinutes, UnaccountedMinutes
FROM dbo.vw_HeatAnalysis
WHERE FurnaceId = '4/2'   -- <- change this, or drop the WHERE for all furnaces
ORDER BY StartTime DESC;
GO

-- ---------------------------------------------------------------------
-- 2. Average cycle time by day, per furnace
-- ---------------------------------------------------------------------
SELECT *
FROM dbo.vw_HeatDaily
ORDER BY FurnaceId, HeatDate DESC;
GO

-- ---------------------------------------------------------------------
-- 3. Tap-to-tap time (how long between the start of one heat and the
--    next) - the number most furnace shops actually care about
-- ---------------------------------------------------------------------
SELECT
    HeatId,
    FurnaceId,
    StartTime,
    HeatMinutes,
    DATEDIFF(MINUTE, StartTime,
             LEAD(StartTime) OVER (PARTITION BY FurnaceId ORDER BY StartTime)) AS MinutesToNextHeat
FROM dbo.vw_HeatAnalysis
WHERE Status = 'COMPLETE'
ORDER BY FurnaceId, StartTime DESC;
GO

-- ---------------------------------------------------------------------
-- 4. Which stage is costing the most time vs. the expected range?
-- ---------------------------------------------------------------------
SELECT
    'scrap_charging'  AS Stage, '20-30'  AS ExpectedMinutes,
    COUNT(*)          AS Heats,
    CAST(AVG(ScrapMinutes) AS DECIMAL(10,2))  AS AvgMinutes,
    CAST(MAX(ScrapMinutes) AS DECIMAL(10,2))  AS WorstMinutes,
    SUM(1 - ScrapWithinExpected)              AS OutsideRange
FROM dbo.vw_HeatAnalysis WHERE HasUnknownStage = 0 AND ScrapMinutes IS NOT NULL
UNION ALL
SELECT
    'sponge_charging', '60-150',
    COUNT(*),
    CAST(AVG(SpongeMinutes) AS DECIMAL(10,2)),
    CAST(MAX(SpongeMinutes) AS DECIMAL(10,2)),
    SUM(1 - SpongeWithinExpected)
FROM dbo.vw_HeatAnalysis WHERE HasUnknownStage = 0 AND SpongeMinutes IS NOT NULL
UNION ALL
SELECT
    'tapping', '10-20',
    COUNT(*),
    CAST(AVG(TappingMinutes) AS DECIMAL(10,2)),
    CAST(MAX(TappingMinutes) AS DECIMAL(10,2)),
    SUM(1 - TappingWithinExpected)
FROM dbo.vw_HeatAnalysis WHERE HasUnknownStage = 0 AND TappingMinutes IS NOT NULL;
GO

-- ---------------------------------------------------------------------
-- 5. Do tilts actually cost time? (correlation check by tilt count)
-- ---------------------------------------------------------------------
SELECT
    FurnaceId,
    TiltCount,
    COUNT(*)                                     AS Heats,
    CAST(AVG(SpongeMinutes) AS DECIMAL(10,2))    AS AvgSpongeMinutes,
    CAST(AVG(HeatMinutes)   AS DECIMAL(10,2))    AS AvgHeatMinutes
FROM dbo.vw_HeatAnalysis
WHERE Status = 'COMPLETE' AND HasUnknownStage = 0
GROUP BY FurnaceId, TiltCount
ORDER BY FurnaceId, TiltCount;
GO

-- ---------------------------------------------------------------------
-- 6. Every event of a single heat, in order (drill-down / audit trail)
--    HeatId is already unique per furnace (it's timestamp-based), but
--    FurnaceId is included here for clarity/confirmation.
-- ---------------------------------------------------------------------
SELECT EventName, FurnaceId, StartTime, EndTime, DurationMinutes, TiltNumber, Note
FROM dbo.HeatEvents
WHERE HeatId = 'Heat_001_20260811_080000'   -- <- change this
ORDER BY EventId;
GO

-- ---------------------------------------------------------------------
-- 7. Throughput by hour of day, per furnace - when does the shop slow down?
-- ---------------------------------------------------------------------
SELECT
    FurnaceId,
    HeatStartHour,
    COUNT(*)                                   AS HeatsStarted,
    CAST(AVG(HeatMinutes) AS DECIMAL(10,2))    AS AvgHeatMinutes
FROM dbo.vw_HeatAnalysis
WHERE Status = 'COMPLETE' AND HasUnknownStage = 0
GROUP BY FurnaceId, HeatStartHour
ORDER BY FurnaceId, HeatStartHour;
GO

-- ---------------------------------------------------------------------
-- 8. Data-quality check: heats still open, or missing a stage
-- ---------------------------------------------------------------------
SELECT HeatId, FurnaceId, Status, StartTime, EndTime, HasUnknownStage,
       ScrapMinutes, SpongeMinutes, TappingMinutes, SourceHost, UpdatedAt
FROM dbo.vw_HeatAnalysis
WHERE Status <> 'COMPLETE'
   OR HasUnknownStage = 1
   OR ScrapMinutes IS NULL
   OR SpongeMinutes IS NULL
   OR TappingMinutes IS NULL
ORDER BY FurnaceId, StartTime DESC;
GO

-- ---------------------------------------------------------------------
-- 9. Furnace vs. furnace - side-by-side comparison once more than one
--    furnace is writing into this database (e.g. 4/2 and 4/1)
-- ---------------------------------------------------------------------
SELECT
    FurnaceId,
    COUNT(*)                                   AS HeatsCompleted,
    CAST(AVG(HeatMinutes)    AS DECIMAL(10,2)) AS AvgHeatMinutes,
    CAST(AVG(ScrapMinutes)   AS DECIMAL(10,2)) AS AvgScrapMinutes,
    CAST(AVG(SpongeMinutes)  AS DECIMAL(10,2)) AS AvgSpongeMinutes,
    CAST(AVG(TappingMinutes) AS DECIMAL(10,2)) AS AvgTappingMinutes,
    SUM(TiltCount)                             AS TotalTilts
FROM dbo.vw_HeatAnalysis
WHERE Status = 'COMPLETE' AND HasUnknownStage = 0
GROUP BY FurnaceId
ORDER BY FurnaceId;
GO