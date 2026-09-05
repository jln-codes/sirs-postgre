from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from osgeo import gdal, osr
import requests


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "weather"
    / "arpege_precipitation_probe.py"
)
SPEC = importlib.util.spec_from_file_location("arpege_precipitation_probe", SCRIPT_PATH)
probe = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


class ArpegePrecipitationProbeTest(unittest.TestCase):
    def test_missing_api_key_exits_cleanly(self):
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(probe, "load_dotenv"),
            patch("sys.stderr", stderr),
        ):
            result = probe.main([])

        self.assertEqual(result, 2)
        self.assertIn(probe.API_KEY_ENV, stderr.getvalue())

    def test_http_error_does_not_expose_api_key(self):
        class Session:
            def get(self, *_args, **_kwargs):
                response = requests.Response()
                response.status_code = 401
                response.url = "https://example.invalid"
                response.raise_for_status()

        secret = "must-not-appear"
        client = probe.WcsClient(secret, session=Session())

        with self.assertRaisesRegex(probe.ProbeError, r"HTTP 401") as caught:
            client.get_capabilities()

        self.assertNotIn(secret, str(caught.exception))

    def test_selects_latest_run_containing_every_required_period(self):
        identifiers = []
        for run in ("2026-09-04T18.00.00Z", "2026-09-05T00.00.00Z"):
            for period in probe.PERIODS:
                if run == "2026-09-05T00.00.00Z" and period == "PT6H":
                    continue
                identifiers.append(
                    f"<wcs:CoverageId>{probe.PRODUCT}___{run}_{period}</wcs:CoverageId>"
                )
        payload = (
            '<wcs:Capabilities xmlns:wcs="http://www.opengis.net/wcs/2.0">'
            + "".join(identifiers)
            + "</wcs:Capabilities>"
        ).encode()

        selected = probe.select_latest_complete_run(probe.parse_capabilities(payload))

        self.assertEqual(set(selected), set(probe.PERIODS))
        self.assertEqual(
            next(iter(selected.values())).run,
            datetime(2026, 9, 4, 18, tzinfo=timezone.utc),
        )

    def test_description_uses_discrete_time_coefficients_and_common_time(self):
        run = datetime(2026, 9, 5, tzinfo=timezone.utc)

        def description(identifier, coefficients):
            return f'''<wcs:CoverageDescriptions
                xmlns:wcs="http://www.opengis.net/wcs/2.0"
                xmlns:gml="http://www.opengis.net/gml/3.2"
                xmlns:gmlrgrid="http://www.opengis.net/gml/3.3/rgrid"
                xmlns:swe="http://www.opengis.net/swe/2.0">
              <wcs:CoverageDescription>
                <gml:beginPosition>2026-09-05T01:00:00Z</gml:beginPosition>
                <gml:endPosition>2026-09-06T06:00:00Z</gml:endPosition>
                <wcs:CoverageId>{identifier}</wcs:CoverageId>
                <gmlrgrid:GeneralGridAxis>
                  <gmlrgrid:coefficients>{coefficients}</gmlrgrid:coefficients>
                  <gmlrgrid:gridAxesSpanned>time</gmlrgrid:gridAxesSpanned>
                </gmlrgrid:GeneralGridAxis>
                <swe:uom code="kg m-2"/>
              </wcs:CoverageDescription>
            </wcs:CoverageDescriptions>'''.encode()

        first = probe.parse_description(description("first", "3600 86400 90000"), run)
        second = probe.parse_description(description("second", "10800 86400 90000"), run)

        self.assertEqual(first.unit, "kg m-2")
        self.assertEqual(
            probe.select_common_time([first, second]),
            datetime(2026, 9, 6, tzinfo=timezone.utc),
        )

    def test_getcoverage_uses_repeated_unquoted_subsets(self):
        class Response:
            status_code = 200
            content = b""

            def raise_for_status(self):
                return None

            def iter_content(self, _size):
                yield b"TIFF"

        class Session:
            def get(self, url, **kwargs):
                self.url = url
                self.kwargs = kwargs
                return Response()

        session = Session()
        client = probe.WcsClient("secret-test", session=session)
        valid_time = datetime(2026, 9, 6, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            destination = Path(directory) / "sample.tif"
            client.download_coverage(
                "coverage", valid_time, probe.DEFAULT_BBOX, destination
            )

        subsets = [
            value for name, value in session.kwargs["params"] if name == "subset"
        ]
        self.assertEqual(subsets[-1], "time(2026-09-06T00:00:00Z)")
        self.assertNotIn('"', subsets[-1])
        self.assertEqual(session.kwargs["headers"], {"apikey": "secret-test"})

    def test_getcoverage_retries_502_then_succeeds(self):
        class Response:
            def __init__(self, status_code):
                self.status_code = status_code

            def iter_content(self, _size):
                yield b"TIFF"

        class Session:
            def __init__(self):
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                return Response(502 if self.calls == 1 else 200)

        sleeps = []
        session = Session()
        client = probe.WcsClient("secret-test", session=session, sleep=sleeps.append)

        stdout = io.StringIO()
        with TemporaryDirectory() as directory, redirect_stdout(stdout):
            client.download_coverage(
                "coverage-502-then-ok",
                datetime(2026, 9, 6, tzinfo=timezone.utc),
                probe.DEFAULT_BBOX,
                Path(directory) / "sample.tif",
            )

        self.assertEqual(session.calls, 2)
        self.assertEqual(sleeps, [1])
        self.assertIn("tentative 1/3, HTTP 502", stdout.getvalue())
        self.assertIn("tentative 2/3, HTTP 200", stdout.getvalue())

    def test_getcoverage_three_502_failures_report_product(self):
        class Response:
            status_code = 502

            def iter_content(self, _size):
                yield b""

        class Session:
            def __init__(self):
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                return Response()

        sleeps = []
        session = Session()
        client = probe.WcsClient("secret-test", session=session, sleep=sleeps.append)

        stdout = io.StringIO()
        with TemporaryDirectory() as directory, redirect_stdout(stdout):
            with self.assertRaisesRegex(
                probe.ProbeError, r"coverage-always-502.*HTTP 502"
            ):
                client.download_coverage(
                    "coverage-always-502",
                    datetime(2026, 9, 6, tzinfo=timezone.utc),
                    probe.DEFAULT_BBOX,
                    Path(directory) / "sample.tif",
                )

        self.assertEqual(session.calls, 3)
        self.assertEqual(sleeps, [1, 2])
        self.assertIn("tentative 3/3, HTTP 502", stdout.getvalue())

    def test_getcoverage_400_does_not_retry(self):
        class Response:
            status_code = 400

            def iter_content(self, _size):
                yield b""

        class Session:
            def __init__(self):
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                return Response()

        session = Session()
        client = probe.WcsClient("secret-test", session=session, sleep=lambda _delay: None)

        with TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(probe.ProbeError, r"HTTP 400"):
                client.download_coverage(
                    "coverage-bad-request",
                    datetime(2026, 9, 6, tzinfo=timezone.utc),
                    probe.DEFAULT_BBOX,
                    Path(directory) / "sample.tif",
                )

        self.assertEqual(session.calls, 1)

    def test_getcoverage_messages_do_not_expose_api_key(self):
        class Response:
            status_code = 502

            def iter_content(self, _size):
                yield b""

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        secret = "must-not-appear"
        client = probe.WcsClient(
            secret,
            session=Session(),
            verbose=True,
            sleep=lambda _delay: None,
        )

        stdout = io.StringIO()
        with TemporaryDirectory() as directory, redirect_stdout(stdout):
            with self.assertRaises(probe.ProbeError) as caught:
                client.download_coverage(
                    "coverage-secret-check",
                    datetime(2026, 9, 6, tzinfo=timezone.utc),
                    probe.DEFAULT_BBOX,
                    Path(directory) / "sample.tif",
                )

        self.assertNotIn(secret, stdout.getvalue())
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("coverageid=coverage-secret-check", stdout.getvalue())

    def test_analyzes_single_band_raster_without_numpy(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.tif"
            driver = gdal.GetDriverByName("GTiff")
            dataset = driver.Create(str(path), 3, 3, 1, gdal.GDT_Float64)
            dataset.SetGeoTransform((2.4, 0.1, 0, 50.7, 0, -0.1))
            spatial_reference = osr.SpatialReference()
            spatial_reference.ImportFromEPSG(4326)
            dataset.SetProjection(spatial_reference.ExportToWkt())
            band = dataset.GetRasterBand(1)
            band.Fill(2.5)
            band.SetMetadataItem("GRIB_ELEMENT", "RPRATE")
            band.SetMetadataItem("GRIB_UNIT", "[kg/(m^2*s)]")
            dataset = None

            summary = probe.analyze_raster(path, "PT1H", "coverage", "kg m-2")

        self.assertEqual((summary.width, summary.height), (3, 3))
        self.assertEqual(summary.crs, "EPSG:4326")
        self.assertAlmostEqual(summary.center, 2.5)
        self.assertAlmostEqual(summary.mean, 2.5)
        self.assertEqual(summary.data_type, "Float64")
        self.assertEqual(summary.band_tags["default"]["GRIB_ELEMENT"], "RPRATE")


if __name__ == "__main__":
    unittest.main()
