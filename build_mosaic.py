#!/usr/bin/env python3
# /// script
# dependencies = ["Pillow"]
# ///
"""Download a 3x3 Virtual Earth tile patch for each CSV coordinate."""

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import subprocess
import sys
from io import BytesIO
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

TILE_SIZE = 256
MAX_MERCATOR_LAT = 85.05112878
NO_TILE_MAX_BYTES = 1200
MAX_QUALITY_CANDIDATES = 4
DEFAULT_MIN_BRIGHTNESS = 35
DEFAULT_FONT_SIZE = 56
PATCH_SIZE = 3 * TILE_SIZE
DEFAULT_FONT = "/usr/share/fonts/TTF/Iosevka-Regular.ttc"
LAPLACIAN_FILTER = ImageFilter.Kernel(
    (3, 3), (0, 1, 0, 1, -4, 1, 0, 1, 0), scale=1, offset=128
)
# This matches the prototype URL in readme.md. Microsoft says not to hardcode tile URLs.
TILE_URL = "https://tiles.virtualearth.net/tiles/a{quadkey}.jpg?g={generation}"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Firefox/156.0"


def read_grid(path):
    cells = {}
    with open(path, newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        required = {"x", "y", "lat", "lon", "quantile", "population"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
        has_ranks = "candidate_rank" in (reader.fieldnames or [])

        for line, row in enumerate(reader, start=2):
            try:
                x, y = int(row["x"]), int(row["y"])
                lat, lon = float(row["lat"]), float(row["lon"])
                population = float(row["population"])
                quantile = float(row["quantile"])
                rank = int(row["candidate_rank"]) if has_ranks else len(cells.get((x, y), [])) + 1
            except (TypeError, ValueError) as error:
                raise ValueError(f"Invalid candidate data on CSV line {line}") from error
            if (x < 0 or y < 0 or rank < 1 or not math.isfinite(lat) or not math.isfinite(lon)
                    or not math.isfinite(population) or population < 0
                    or not math.isfinite(quantile) or not 0 <= quantile <= 1):
                raise ValueError(f"Invalid candidate data on CSV line {line}")
            if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError(f"Coordinates out of range on CSV line {line}")
            cells.setdefault((x, y), []).append((rank, lat, lon, population, quantile))

    if not cells:
        raise ValueError("CSV has no data rows")

    width = max(x for x, _ in cells) + 1
    height = max(y for _, y in cells) + 1
    expected = {(x, y) for y in range(height) for x in range(width)}
    missing = expected - cells.keys()
    if missing:
        raise ValueError(f"CSV grid has missing positions, such as {min(missing)}")
    return {position: sorted(candidates) for position, candidates in cells.items()}, width, height


def tile_xy(lat, lon, zoom):
    lat = max(-MAX_MERCATOR_LAT, min(MAX_MERCATOR_LAT, lat))
    scale = 1 << zoom
    x = int((lon + 180.0) / 360.0 * scale)
    lat_radians = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_radians)) / math.pi) / 2.0 * scale)
    return min(scale - 1, max(0, x)), min(scale - 1, max(0, y))


def quadkey_for(x, y, zoom):
    key = []
    for level in range(zoom, 0, -1):
        mask = 1 << (level - 1)
        key.append(str((1 if x & mask else 0) + (2 if y & mask else 0)))
    return "".join(key)


def download_tile(quadkey, generation):
    url = TILE_URL.format(quadkey=quadkey, generation=generation)
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        data = response.read()
        no_tile = response.headers.get("X-VE-Tile-Info", "").lower() == "no-tile"
        small_png = response.headers.get_content_type() == "image/png" and len(data) <= NO_TILE_MAX_BYTES
        if no_tile or small_png:
            return None
        return Image.open(BytesIO(data)).convert("RGB")


def cached_tile(quadkey, generation, cache):
    if quadkey not in cache:
        cache[quadkey] = download_tile(quadkey, generation)
    return cache[quadkey]


def candidate_center(lat, lon, zoom, generation, cache):
    x, y = tile_xy(lat, lon, zoom)
    key = quadkey_for(x, y, zoom)
    return x, y, cached_tile(key, generation, cache)


def candidate_patch(x, y, zoom, generation, cache, executor):
    scale = 1 << zoom
    positions = []
    for row, dy in enumerate((-1, 0, 1)):
        tile_y = y + dy
        if not 0 <= tile_y < scale:
            return None
        for column, dx in enumerate((-1, 0, 1)):
            tile_x = (x + dx) % scale
            positions.append((column, row, quadkey_for(tile_x, tile_y, zoom)))

    missing = list(dict.fromkeys(key for _, _, key in positions if key not in cache))
    futures = {key: executor.submit(download_tile, key, generation) for key in missing}
    for key, future in futures.items():
        cache[key] = future.result()
    if any(cache[key] is None for _, _, key in positions):
        return None

    patch = Image.new("RGB", (PATCH_SIZE, PATCH_SIZE))
    center = None
    for column, row, key in positions:
        tile = cache[key]
        patch.paste(tile, (column * TILE_SIZE, row * TILE_SIZE))
        if column == 1 and row == 1:
            center = tile
    return patch, center


def sharpness_score(tile):
    # This is a rough blur check. Low-detail areas can also score low.
    laplacian = tile.convert("L").filter(LAPLACIAN_FILTER)
    return ImageStat.Stat(laplacian).var[0]


def mean_brightness(image):
    return ImageStat.Stat(image.convert("L")).mean[0]


GEOCODE_SCRIPT = """\
import {cellToLatLng, gridDisk, latLngToCell} from "h3-js";
import {findClosestCities} from "tiny-geocoder";
import {closestCity, mostPopulousCityInCell} from "/home/olie/projects/H3-MON/src/city-label.js";

const {points, resolution} = JSON.parse(await new Response(Bun.stdin.stream()).text());
const labels = points.map(({lat, lon}) => {
  const index = latLngToCell(lat, lon, resolution);
  const center = cellToLatLng(index);
  const neighbors = gridDisk(index, 1).filter(cell => cell !== index).map(cellToLatLng);
  const cities = findClosestCities(center[0], center[1], 20);
  return (mostPopulousCityInCell(cities, center, neighbors) || closestCity(cities, center))?.name ?? null;
});
console.log(JSON.stringify(labels));
"""


def reverse_geocode(points, resolution):
    try:
        result = subprocess.run(
            ["bun", "-e", GEOCODE_SCRIPT],
            input=json.dumps({"points": points, "resolution": resolution}),
            text=True, capture_output=True, check=True,
            cwd=Path(__file__).resolve().parent,
        )
    except FileNotFoundError as error:
        raise RuntimeError("Bun is required for reverse geocoding") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip()
        raise RuntimeError(f"Reverse geocoding failed: {detail}") from error
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Reverse geocoding returned invalid data") from error


def format_population(population):
    if population.is_integer():
        return f"{population:,.0f}"
    return f"{population:,.1f}".rstrip("0").rstrip(".")


def percentile_label(quantile):
    percentile = round(quantile * 100)
    suffix = "th" if 10 <= percentile % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(percentile % 10, "th")
    return f"{percentile}{suffix} percentile"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", nargs="?", default="density-tiles.csv", help="Input CSV")
    parser.add_argument("-o", "--output", default="density-mosaic.jpg", help="Output image")
    parser.add_argument("-z", "--zoom", type=int, default=17, help="Tile zoom level (default: 17; lower is farther out)")
    parser.add_argument("--generation", default="45", help="Virtual Earth tile generation (default: 45)")
    parser.add_argument(
        "--min-sharpness", type=float, default=1200,
        help="Try another candidate below this score (default: 1200; 0 disables)",
    )
    parser.add_argument(
        "--min-brightness", type=float, default=DEFAULT_MIN_BRIGHTNESS,
        help="Reject dark imagery below this mean brightness (default: 35; 0 disables)",
    )
    parser.add_argument("--h3-resolution", type=int, default=7, help="H3 resolution for city labels (default: 7)")
    parser.add_argument("--font", default=DEFAULT_FONT, help="Iosevka font file")
    parser.add_argument("--font-size", type=int, default=DEFAULT_FONT_SIZE, help="Label font size (default: 56)")
    args = parser.parse_args()
    if not 1 <= args.zoom <= 23:
        parser.error("zoom must be between 1 and 23")
    if args.min_sharpness < 0 or args.min_brightness < 0:
        parser.error("image thresholds must not be negative")
    if not 0 <= args.h3_resolution <= 15 or args.font_size < 1:
        parser.error("invalid H3 resolution or font size")

    font = ImageFont.truetype(args.font, args.font_size)
    cells, width, height = read_grid(args.csv)
    tiles, sharpness, selected_cells = {}, {}, {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        for (x, y), candidates in sorted(cells.items(), key=lambda item: (item[0][1], item[0][0])):
            probed, samples = set(), []
            bright_count = 0
            for candidate in candidates:
                rank, lat, lon, _, _ = candidate
                tile_x, tile_y, center = candidate_center(lat, lon, args.zoom, args.generation, tiles)
                probed.add(rank)
                if center is None:
                    print(f"No imagery for x={x}, y={y}, candidate {rank}", file=sys.stderr)
                    continue
                key = quadkey_for(tile_x, tile_y, args.zoom)
                if key not in sharpness:
                    sharpness[key] = sharpness_score(center)
                brightness = mean_brightness(center)
                samples.append((candidate, tile_x, tile_y, sharpness[key], brightness))
                if brightness >= args.min_brightness:
                    bright_count += 1
                    if sharpness[key] >= args.min_sharpness or bright_count >= MAX_QUALITY_CANDIDATES:
                        break

            bright_samples = [item for item in samples if item[4] >= args.min_brightness]
            passing = [item for item in bright_samples if item[3] >= args.min_sharpness]
            if passing:
                best = passing[0]
                preferred = [best] + sorted(
                    (item for item in bright_samples if item[0][0] != best[0][0]),
                    key=lambda item: -item[3],
                )
            elif bright_samples:
                preferred = sorted(bright_samples, key=lambda item: -item[3])
            else:
                preferred = sorted(samples, key=lambda item: -item[4])

            attempts = [item[:4] for item in preferred]
            attempts.extend((candidate, None, None, None)
                            for candidate in candidates if candidate[0] not in probed)
            selected = None
            darkest_fallback = None
            for candidate, tile_x, tile_y, score in attempts:
                rank, lat, lon, _, _ = candidate
                if tile_x is None:
                    tile_x, tile_y, center = candidate_center(lat, lon, args.zoom, args.generation, tiles)
                    if center is None:
                        print(f"No imagery for x={x}, y={y}, candidate {rank}", file=sys.stderr)
                        continue
                    key = quadkey_for(tile_x, tile_y, args.zoom)
                    if key not in sharpness:
                        sharpness[key] = sharpness_score(center)
                    score = sharpness[key]
                result = candidate_patch(tile_x, tile_y, args.zoom, args.generation, tiles, executor)
                if result is None:
                    print(f"No complete 3x3 patch for x={x}, y={y}, candidate {rank}", file=sys.stderr)
                    continue
                patch, _ = result
                brightness = mean_brightness(patch)
                if brightness < args.min_brightness:
                    print(f"Dark imagery for x={x}, y={y}, candidate {rank}; trying another", file=sys.stderr)
                    if darkest_fallback is None or brightness > darkest_fallback[2]:
                        darkest_fallback = (patch, candidate, brightness, score)
                    continue
                selected = (patch, candidate, score)
                break

            if selected is None and darkest_fallback is not None:
                patch, candidate, _, score = darkest_fallback
                selected = (patch, candidate, score)
                print(f"Using darkest available imagery for x={x}, y={y}", file=sys.stderr)
            if selected is None:
                raise ValueError(f"No complete 3x3 patch for x={x}, y={y} after trying {len(candidates)} candidates")

            patch, candidate, score = selected
            rank = candidate[0]
            if score < args.min_sharpness:
                print(f"Best candidate for x={x}, y={y} is below the sharpness threshold (score {score:.0f})", file=sys.stderr)
            elif rank > 1:
                print(f"Using fallback candidate {rank} for x={x}, y={y}", file=sys.stderr)
            selected_cells[(x, y)] = (patch, candidate)

    positions = sorted(selected_cells, key=lambda point: (point[1], point[0]))
    points = [{"lat": selected_cells[position][1][1], "lon": selected_cells[position][1][2]}
              for position in positions]
    labels = reverse_geocode(points, args.h3_resolution)
    if len(labels) != len(positions):
        raise RuntimeError("Reverse geocoder returned the wrong number of labels")

    mosaic = Image.new("RGB", (width * PATCH_SIZE, height * PATCH_SIZE))
    for (x, y), label in zip(positions, labels):
        patch, candidate = selected_cells[(x, y)]
        draw = ImageDraw.Draw(patch)
        text_options = {"font": font, "fill": "white", "stroke_width": 4, "stroke_fill": "black"}
        caption = (
            percentile_label(candidate[4]),
            label or "Unknown city",
            f"{format_population(candidate[3])} people/km²",
        )
        for line, text in enumerate(caption):
            draw.text((16, 14 + line * (args.font_size + 8)), text, **text_options)
        mosaic.paste(patch, (x * PATCH_SIZE, y * PATCH_SIZE))

    mosaic.save(args.output)
    print(f"Wrote {args.output} ({width * PATCH_SIZE}x{height * PATCH_SIZE})")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
