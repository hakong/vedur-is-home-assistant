#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source_image="$root/branding/mark.png"
output="$root/custom_components/vedur_is/brand"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

command -v magick >/dev/null
fonts="$(magick -list font)"
if [[ "$fonts" != *'Font: NimbusSans-Regular'* ]]; then
  printf '%s\n' 'Install the URW Base35 Nimbus Sans fonts before rebuilding.' >&2
  exit 1
fi
mkdir -p -- "$output"

magick "$source_image" -colorspace sRGB \
  -channel A -level 2%,100% +channel -trim +repage "$work/master.png"
magick "$work/master.png" -alpha extract "$work/alpha.png"
magick -size 1x1 xc:'#004C5B' xc:white xc:'#FFD23F' +append "$work/palette.png"
magick "$work/master.png" -background '#00515B' -alpha remove -alpha off \
  -dither None -remap "$work/palette.png" \
  "$work/alpha.png" -compose CopyOpacity -composite \
  "$work/flat-mark.png"

for theme in light dark; do
  prefix=""
  text_color="#183B44"
  subtitle_color="#42636B"
  color_options=()
  if [[ "$theme" == dark ]]; then
    prefix="dark_"
    text_color="#F1F7F7"
    subtitle_color="#B8D6D8"
    color_options=(-channel RGB -fuzz 35% -fill '#258B96' -opaque '#004A58' +channel)
  fi

  magick "$work/flat-mark.png" \
    "${color_options[@]}" "$work/mark.png"

  for size in 256 512; do
    suffix=""
    if [[ "$size" == 512 ]]; then suffix="@2x"; fi
    magick "$work/mark.png" -filter Lanczos -resize "${size}x${size}" \
      -gravity center -background none -extent "${size}x${size}" \
      -strip -depth 8 -interlace PNG -define png:color-type=6 \
      -define png:compression-level=9 "$output/${prefix}icon${suffix}.png"
  done

  magick "$work/mark.png" -filter Lanczos -resize x512 "$work/logo-mark.png"
  magick -background none -fill "$text_color" -font NimbusSans-Regular \
    -pointsize 132 label:'Icelandic Weather' -trim +repage "$work/title.png"
  magick -background none -fill "$subtitle_color" -font NimbusSans-Regular \
    -pointsize 70 label:'Unofficial integration' -trim +repage "$work/subtitle.png"
  magick "$work/title.png" -background none -gravity south \
    -splice 0x38 "$work/subtitle.png" -compose Over -append "$work/wordmark.png"
  magick "$work/logo-mark.png" -background none -gravity east \
    -splice 64x0 "$work/wordmark.png" -gravity center -compose Over +append \
    -trim +repage -strip -depth 8 -interlace PNG \
    -define png:color-type=6 -define png:compression-level=9 \
    "$output/${prefix}logo@2x.png"
  magick "$output/${prefix}logo@2x.png" -filter Lanczos -resize x256 \
    -strip -depth 8 -interlace PNG -define png:color-type=6 \
    -define png:compression-level=9 "$output/${prefix}logo.png"
done
