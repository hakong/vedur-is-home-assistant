"""Validate local branding and Home Assistant's offline brand-image serving."""

from collections import deque
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageChops

try:
    from aiohttp.test_utils import make_mocked_request
    from homeassistant.components.brands import BrandsIntegrationView
    from homeassistant.components.brands.const import ALLOWED_IMAGES
    from homeassistant.components.http import KEY_AUTHENTICATED
    from homeassistant.core import HomeAssistant
except ModuleNotFoundError as error:
    if error.name not in {"homeassistant", "homeassistant.components.brands"}:
        raise
    HomeAssistant = None


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "vedur_is"
BRAND = INTEGRATION / "brand"
EXPECTED_IMAGES = {
    f"{theme}{kind}{scale}.png"
    for theme in ("", "dark_")
    for kind in ("icon", "logo")
    for scale in ("", "@2x")
}


class TestBrandImages(unittest.TestCase):
    """Check the image requirements without needing a Home Assistant runtime."""

    def test_all_eight_supported_variants_are_present(self):
        self.assertEqual({path.name for path in BRAND.iterdir()}, EXPECTED_IMAGES)

    def test_setup_notice_is_explicit_and_matches_the_english_translation(self):
        descriptions = [
            json.loads(path.read_text())["config"]["step"]["menu"]["description"]
            for path in (
                INTEGRATION / "strings.json",
                INTEGRATION / "translations/en.json",
            )
        ]
        self.assertEqual(descriptions[0], descriptions[1])
        self.assertIn("Unofficial community integration.", descriptions[0])
        self.assertIn("Not created, maintained, or endorsed by", descriptions[0])

    def test_png_dimensions_transparency_and_optimization(self):
        for filename in sorted(EXPECTED_IMAGES):
            with self.subTest(image=filename), Image.open(BRAND / filename) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.mode, "RGBA")
                self.assertEqual(image.getchannel("A").getextrema(), (0, 255))
                self.assertEqual(image.info.get("interlace"), 1)
                self.assertNotIn("icc_profile", image.info)
                self.assertNotIn("exif", image.info)
                size = 512 if "@2x" in filename else 256
                if "icon" in filename:
                    self.assertEqual(image.size, (size, size))
                else:
                    self.assertGreater(image.width, image.height)
                    self.assertEqual(image.height, size)
                # Necessary square padding is allowed; the image must fill an axis.
                left, top, right, bottom = image.getchannel("A").getbbox()
                self.assertTrue(
                    (left == 0 and right == image.width)
                    or (top == 0 and bottom == image.height)
                )
                with Image.open(BRAND / filename) as encoded:
                    encoded.verify()

    def test_dark_variants_preserve_geometry_but_change_colours(self):
        for kind in ("icon", "logo"):
            for scale in ("", "@2x"):
                with self.subTest(kind=kind, scale=scale):
                    with (
                        Image.open(BRAND / f"{kind}{scale}.png") as light,
                        Image.open(BRAND / f"dark_{kind}{scale}.png") as dark,
                    ):
                        self.assertEqual(light.size, dark.size)
                        self.assertIsNone(
                            ImageChops.difference(
                                light.getchannel("A"), dark.getchannel("A")
                            ).getbbox()
                        )
                        self.assertIsNotNone(
                            ImageChops.difference(
                                light.convert("RGB"), dark.convert("RGB")
                            ).getbbox()
                        )

    def test_retina_variants_preserve_proportions(self):
        for theme in ("", "dark_"):
            for kind in ("icon", "logo"):
                with self.subTest(theme=theme, kind=kind):
                    with (
                        Image.open(BRAND / f"{theme}{kind}.png") as normal,
                        Image.open(BRAND / f"{theme}{kind}@2x.png") as retina,
                    ):
                        self.assertEqual(retina.height, normal.height * 2)
                        self.assertLessEqual(abs(retina.width - normal.width * 2), 1)


@unittest.skipIf(HomeAssistant is None, "requires Home Assistant 2026.3+ brands API")
class TestLocalBrandServing(unittest.IsolatedAsyncioTestCase):
    """Exercise HA's real brands view, with only integration discovery stubbed."""

    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.hass = HomeAssistant(directory.name)
        self.hass.data["brands"] = deque(["test-brand-token"])
        self.view = BrandsIntegrationView(self.hass)

    async def test_all_variants_are_served_locally_without_a_cdn(self):
        integration = SimpleNamespace(file_path=INTEGRATION, has_branding=True)
        with (
            patch(
                "homeassistant.components.brands.async_get_custom_components",
                new=AsyncMock(return_value={"vedur_is": integration}),
            ),
            patch.object(
                self.view,
                "_get_image_data",
                new=AsyncMock(side_effect=AssertionError("unexpected CDN lookup")),
            ),
        ):
            for filename in sorted(EXPECTED_IMAGES):
                with self.subTest(image=filename):
                    self.assertIn(filename, ALLOWED_IMAGES)
                    request = make_mocked_request(
                        "GET", f"/api/brands/integration/vedur_is/{filename}"
                    )
                    request[KEY_AUTHENTICATED] = True
                    response = await self.view.get(request, "vedur_is", filename)
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.content_type, "image/png")
                    self.assertEqual(response.body, (BRAND / filename).read_bytes())

    async def test_signed_brand_token_is_accepted(self):
        integration = SimpleNamespace(file_path=INTEGRATION, has_branding=True)
        with patch(
            "homeassistant.components.brands.async_get_custom_components",
            new=AsyncMock(return_value={"vedur_is": integration}),
        ):
            request = make_mocked_request(
                "GET",
                "/api/brands/integration/vedur_is/icon.png?token=test-brand-token",
            )
            request[KEY_AUTHENTICATED] = False
            response = await self.view.get(request, "vedur_is", "icon.png")
            self.assertEqual(response.body, (BRAND / "icon.png").read_bytes())
