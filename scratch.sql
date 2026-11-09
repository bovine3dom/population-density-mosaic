-- ClickHouse. Set the mosaic dimensions here. Coordinates are zero-based.
WITH
    -- Set this to 'world' to skip the country filter.
    'CG' AS selected_country,
    -- Geonames place records approximate the country in each res-4 cell.
    country_cells AS
    (
        SELECT
            h3ToParent(h3, 4) AS parent_h3,
            anyHeavy(country_code) AS dominant_country
        FROM public_geonames
        WHERE selected_country != 'world'
            AND feature_class = 'P'
            AND country_code != ''
        GROUP BY parent_h3
    ),
    pop_table as (
        select median(population) population, h3ToParent(h3, 7) h3
        from public_kontur_population_20231101
        WHERE selected_country = 'world'
            OR h3ToParent(h3, 4) IN (
                SELECT parent_h3 FROM country_cells
                WHERE dominant_country = selected_country
            )
        group by all
    ),
    5 AS grid_width,
    5 AS grid_height,
    50 AS candidates_per_cell,
    grid_width * grid_height AS cell_count,
    population_by_value AS
    (
        -- Use population as both the value and its weight.
        SELECT
            population,
            sum(toUInt64(population)) AS weight
        FROM pop_table
        WHERE population > 0
        GROUP BY population
        HAVING weight > 0
    ),
    population_cdf AS
    (
        SELECT
            population,
            sum(weight) OVER (
                ORDER BY population
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ) AS cumulative_weight,
            sum(weight) OVER () AS total_weight
        FROM population_by_value
    ),
    quantile_targets AS
    (
        -- Place quantiles at the center of each grid cell.
        SELECT
            number AS i,
            (number + 0.5) / cell_count AS quantile
        FROM numbers(cell_count)
    ),
    quantile_cells AS
    (
        SELECT
            targets.i,
            targets.quantile,
            minIf(
                cdf.population,
                cdf.cumulative_weight >= targets.quantile * cdf.total_weight
            ) AS target_population
        FROM quantile_targets AS targets
        CROSS JOIN population_cdf AS cdf
        GROUP BY targets.i, targets.quantile
    ),
    ranked_candidates AS
    (
        -- Hash ties per grid cell to avoid bias from H3's geographic ordering.
        SELECT
            targets.i,
            targets.quantile,
            targets.target_population,
            candidates.population,
            candidates.h3,
            row_number() OVER (
                PARTITION BY targets.i
                ORDER BY
                    abs(toFloat64(candidates.population) - toFloat64(targets.target_population)),
                    cityHash64(concat(toString(candidates.h3), ':', toString(targets.i))),
                    candidates.h3
            ) AS candidate_rank
        FROM quantile_cells AS targets
        CROSS JOIN pop_table AS candidates
        WHERE candidates.population > 0
    )
SELECT
    modulo(i, grid_width) AS x,
    intDiv(i, grid_width) AS y,
    tupleElement(h3ToGeo(h3), 1) AS lat,
    tupleElement(h3ToGeo(h3), 2) AS lon,
    quantile,
    population,
    target_population,
    candidate_rank
FROM ranked_candidates
WHERE candidate_rank <= candidates_per_cell
ORDER BY i, candidate_rank
INTO OUTFILE 'density-tiles.csv'
TRUNCATE
FORMAT CSVWithNames;
