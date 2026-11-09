idea:

mosaic of population weighted population density, from left to right in rows and then top to bottom, let's say 4x4 for now as a first attempt.

need to fetch tiles from virtual earth and stitch them together given some lat/lons. we can play with tuneable zoom. whatever

curl 'https://tiles.virtualearth.net/tiles/a03113323321131031.jpg?g=45' \
  -H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:156.0) Gecko/20100101 Firefox/156.0' \
  -H 'Accept: image/webp,*/*' \
  -H 'Accept-Language: en-GB,en;q=0.9' \
  -H 'Accept-Encoding: gzip, deflate, br, zstd' \
  -H 'Referer: https://compute.olie.science/' \
  -H 'Origin: https://compute.olie.science' \
  -H 'Connection: keep-alive' \
  -H 'Sec-Fetch-Dest: empty' \
  -H 'Sec-Fetch-Mode: cors' \
  -H 'Sec-Fetch-Site: cross-site' \
  -H 'Priority: u=4' \
  -H 'Pragma: no-cache' \
  -H 'Cache-Control: no-cache' \
  -H 'TE: trailers'

  zoom level is. (quadtree i think?)


in clickhouse there's some window function you can use, percent rank? but can't weight it, so best to do it arse backwards, get the weighted quantiles first and then find nearby ones

would also need to use natural earth H3 first to trim down to per country (but actually it would be pretty cool to do it at planet scale?)
