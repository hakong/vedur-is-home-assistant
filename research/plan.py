"""Explicit endpoint inventory and repeatable, bounded polling plan."""

from urllib.parse import urlencode

OFFICIAL = "https://api.vedur.is/weather"
GOTT = "https://www.gottvedur.is"
XML = "https://xmlweather.vedur.is/"
DETAIL_STATIONS = (1470, 1474, 2631, 1868, 6222, 6459, 6472, 2693)


def build_jobs(stations):
    physical = [s["station_id"] for s in stations if s["type"] != "virtual"]
    all_ids = [s["station_id"] for s in stations]
    jobs = []

    def add(name, source, kind, url, interval, ids=(), body=None, hour_offset=None):
        jobs.append(
            dict(
                id=name,
                source=source,
                kind=kind,
                url=url,
                interval=interval,
                station_ids=list(ids),
                body=body,
                hour_offset=hour_offset,
            )
        )

    for aggregate in ("10min", "hour"):
        path = f"/observations/aws/{aggregate}/latest"
        query = [("parameters", "all")] + [("station_id", sid) for sid in physical]
        add(
            f"official_{aggregate}",
            "official",
            "official_observation",
            OFFICIAL + path + "?" + urlencode(query),
            300,
            physical,
        )
    for name, path in (
        ("raw", "/observations/aws/raw/10min/latest"),
        ("cloud", "/observations/aws/raw/cloud/1min/latest"),
        ("synop", "/observations/synop/latest"),
    ):
        add(
            "official_" + name,
            "official",
            "official_observation",
            OFFICIAL
            + path
            + "?"
            + urlencode([("station_id", sid) for sid in physical]),
            300,
            physical,
        )
    for kind, interval in (("obs", 300), ("forec", 900)):
        for language in ("en", "is"):
            ids = physical if kind == "obs" else all_ids
            add(
                f"xml_{kind}_{language}",
                "xml",
                "xml_" + kind,
                XML
                + "?"
                + urlencode(
                    dict(
                        op_w="xml",
                        type=kind,
                        lang=language,
                        view="xml",
                        ids=";".join(map(str, ids)),
                    )
                ),
                interval,
                ids,
            )
    for sid in physical:
        add(
            f"gott_latest_{sid}",
            "gottvedur",
            "gott_observation",
            GOTT + f"/api/observations/latest/?stationId={sid}",
            300,
            [sid],
        )
    for sid in all_ids:
        add(
            f"gott_forecast_{sid}",
            "gottvedur",
            "gott_forecast",
            GOTT + f"/api/forecasts/highChart/?stationId={sid}",
            900,
            [sid],
        )
    for sid in DETAIL_STATIONS:
        for label, offset, interval in (("current", 1, 900), ("previous", 0, 3600)):
            add(
                f"gott_details_{label}_{sid}",
                "gottvedur",
                "detail",
                GOTT + f"/api/observations/detailsByHour/?stationId={sid}",
                interval,
                [sid],
                hour_offset=offset,
            )
    for endpoint, interval, kind in (
        ("observations/observationStations", 300, "gott_batch_observation"),
        ("forecasts/forecastStations", 900, "gott_batch_forecast"),
    ):
        add(
            "gott_" + endpoint.split("/")[1],
            "gottvedur",
            kind,
            GOTT + "/api/" + endpoint + "/",
            interval,
            all_ids,
            {"ids": all_ids},
        )
    for path in (
        "/openapi.json",
        "/stations",
        "/stations?active=true",
        "/capabilities",
    ):
        add("metadata_" + path, "official", "metadata", OFFICIAL + path, 86400)
    for path in (
        "/observations/aws/10min/latest",
        "/observations/aws/hour/latest",
        "/observations/aws/raw/10min/latest",
        "/observations/aws/raw/cloud/1min/latest",
        "/observations/synop/latest",
    ):
        for language in ("en", "is"):
            add(
                "parameters_" + language + path,
                "official",
                "metadata",
                OFFICIAL + "/parameters?" + urlencode(dict(url=path, locale=language)),
                86400,
            )
    for kind in ("observation", "forecast"):
        add(
            "gott_map_" + kind,
            "gottvedur",
            "metadata",
            GOTT
            + f"/api/map/{kind}/boundary?"
            + urlencode(dict(x1=-25, x2=-12, y1=62, y2=68, zoom=18)),
            21600,
        )
    return jobs


def make_config(stations):
    return {
        "schema_version": 1,
        "duration_days": 14,
        "minimum_request_gap_seconds": 2,
        "timeout_seconds": 30,
        "maximum_response_bytes": 8 * 1024 * 1024,
        "maximum_archive_bytes": 4 * 1024**3,
        "minimum_free_bytes": 2 * 1024**3,
        "user_agent": "vedur-is-research/0.1 (+https://github.com/hakong/vedur-is-home-assistant)",
        "station_seed_date": "2026-10-24",
        "stations": stations,
        "jobs": build_jobs(stations),
    }


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stations", type=Path, default=Path(__file__).with_name("stations.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = make_config(json.loads(args.stations.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        handle.write(json.dumps(config, indent=2) + "\n")
    print(
        f"Created {len(config['jobs'])} jobs; nominal {sum(3600 / job['interval'] for job in config['jobs']):.1f} requests/hour before backoff"
    )
