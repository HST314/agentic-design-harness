from __future__ import annotations

import io
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pypdfium2 as pdfium
from harness.core.errors import HarnessError
from harness.domain.service import TaskCommandService
from harness.services.asset_tools import AssetToolRegistry
from harness.services.asset_understanding import AssetUnderstandingService
from harness.services.assets import AssetService
from harness.services.model_clients import ModelResult, ModelUsage
from harness.services.task_config import TaskConfigService
from harness.services.usage import UsageService
from harness.storage.store import FileStateStore
from PIL import Image
from runtime_helpers import build_config_snapshot, build_service, create_task


class FakeVisionClient:
    def __init__(self, calls: list[dict]) -> None:
        self.calls = calls

    def inspect_image(self, **kwargs):
        self.calls.append(kwargs)
        return ModelResult(
            request_id=f"vision_request_{len(self.calls)}",
            provider_request_id=f"provider_vision_{len(self.calls)}",
            provider="ark",
            model="vision-model",
            call_type="vision_language_model",
            output={
                "summary": "页面包含蓝色品牌标志和标题。",
                "blocks": [
                    {
                        "kind": "image_description",
                        "text": "蓝色标志位于页面中央。",
                        "bbox": [0.2, 0.2, 0.8, 0.8],
                        "confidence": 0.95,
                    }
                ],
                "warnings": [],
            },
            tool_calls=(),
            usage=ModelUsage(20, 10, 0, 0, 30, {"prompt_tokens": 20}),
        )


class FakeModelFactory:
    def __init__(self) -> None:
        self.vision_calls: list[dict] = []

    def vision(self, snapshot, model_id, *, timeout_seconds):
        return FakeVisionClient(self.vision_calls)

    def text(self, snapshot, model_id, *, timeout_seconds):
        raise AssertionError("text client is not used by asset parsing")


@contextmanager
def running_service(
    root: Path,
) -> Iterator[tuple[FileStateStore, TaskCommandService]]:
    store, commands = build_service(root)
    try:
        yield store, commands
    finally:
        store.close()


class AssetUnderstandingTests(unittest.TestCase):
    def test_asset_catalog_enforces_the_task_file_limit_before_parsing(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            running_service(Path(temporary)) as (store, commands),
        ):
            create_task(commands, "task_asset_limit")
            assets = AssetService(store)
            config = TaskConfigService(
                store, build_config_snapshot(max_files_per_task=1)
            )
            understanding = AssetUnderstandingService(
                assets, config, FakeModelFactory(), UsageService(store)
            )
            self._import(
                assets, "task_asset_limit", "one.txt", b"one", "asset-limit-one"
            )
            self._import(
                assets, "task_asset_limit", "two.txt", b"two", "asset-limit-two"
            )

            with self.assertRaises(HarnessError) as raised:
                AssetToolRegistry(assets, understanding).execute(
                    "task_asset_limit",
                    "list_assets",
                    {},
                    idempotency_key="catalog-limit",
                )
            self.assertEqual(raised.exception.code, "ASSET_VALIDATION_FAILED")

    def test_text_markdown_image_and_pdf_routes_are_traceable_and_cached(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            running_service(Path(temporary)) as (store, commands),
        ):
            create_task(commands, "task_understanding")
            assets = AssetService(store)
            factory = FakeModelFactory()
            config = TaskConfigService(store, build_config_snapshot())
            service = AssetUnderstandingService(
                assets, config, factory, UsageService(store)
            )
            text = self._import(
                assets,
                "task_understanding",
                "notes.txt",
                b"Primary color is blue.\n\nKeep the logo clear.",
                "text-asset",
            )
            markdown = self._import(
                assets,
                "task_understanding",
                "brief.md",
                b"# Campaign\n\nUse restrained typography.",
                "markdown-asset",
            )
            image = self._import(
                assets,
                "task_understanding",
                "reference.png",
                self._image_bytes("PNG"),
                "image-asset",
            )
            digital_pdf = self._import(
                assets,
                "task_understanding",
                "digital.pdf",
                self._text_pdf("Digital PDF brand guidance with enough searchable content."),
                "digital-pdf",
            )
            scanned_pdf = self._import(
                assets,
                "task_understanding",
                "scan.pdf",
                self._image_pdf(page_count=1),
                "scan-pdf",
            )

            text_result = service.understand("task_understanding", text["asset_id"])
            markdown_result = service.understand(
                "task_understanding", markdown["asset_id"]
            )
            image_result = service.understand("task_understanding", image["asset_id"])
            digital_result = service.understand(
                "task_understanding", digital_pdf["asset_id"]
            )
            scanned_result = service.understand(
                "task_understanding", scanned_pdf["asset_id"]
            )

            self.assertEqual(text_result["blocks"][0]["extraction_method"], "utf8")
            self.assertEqual(markdown_result["blocks"][0]["kind"], "heading")
            self.assertEqual(image_result["blocks"][0]["extraction_method"], "vlm")
            self.assertTrue(
                any(
                    block["extraction_method"] == "embedded_text"
                    for block in digital_result["blocks"]
                )
            )
            self.assertTrue(
                any(
                    block["extraction_method"] == "vlm"
                    for block in scanned_result["blocks"]
                )
            )
            vision_count = len(factory.vision_calls)
            service.understand("task_understanding", scanned_pdf["asset_id"])
            self.assertEqual(len(factory.vision_calls), vision_count)

            snapshot_path = (
                store.layout.control_root
                / "tasks"
                / "task_understanding"
                / "master"
                / "config-snapshot.json"
            )
            snapshot_text = snapshot_path.read_text(encoding="utf-8")
            self.assertNotIn("test-provider-secret-value", snapshot_text)
            self.assertNotIn("api_key", snapshot_text)
            usage = store.usage.list("task_understanding")
            self.assertGreaterEqual(len(usage), 2)
            self.assertTrue(all(item["agent_type"] == "master" for item in usage))

    def test_bad_encrypted_and_over_limit_pdfs_have_business_warnings(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            running_service(Path(temporary)) as (store, commands),
        ):
            create_task(commands, "task_pdf_errors")
            assets = AssetService(store)
            config = TaskConfigService(
                store, build_config_snapshot(max_pdf_pages=1)
            )
            service = AssetUnderstandingService(
                assets, config, FakeModelFactory(), UsageService(store)
            )
            bad = self._import(
                assets,
                "task_pdf_errors",
                "bad.pdf",
                b"%PDF-1.7\ninvalid",
                "bad-pdf",
            )
            too_long = self._import(
                assets,
                "task_pdf_errors",
                "long.pdf",
                self._image_pdf(page_count=2),
                "long-pdf",
            )
            bad_result = service.understand("task_pdf_errors", bad["asset_id"])
            long_result = service.understand("task_pdf_errors", too_long["asset_id"])
            self.assertEqual(bad_result["status"], "FAILED")
            self.assertEqual(bad_result["warnings"][0]["code"], "PDF_INVALID")
            self.assertEqual(long_result["status"], "FAILED")
            self.assertEqual(long_result["warnings"][0]["code"], "PDF_PAGE_LIMIT")

            encrypted = self._import(
                assets,
                "task_pdf_errors",
                "encrypted.pdf",
                self._image_pdf(page_count=1),
                "encrypted-pdf",
            )
            error = pdfium.PdfiumError(
                "Incorrect password error", err_code=pdfium.raw.FPDF_ERR_PASSWORD
            )
            with patch(
                "harness.services.asset_understanding.pdfium.PdfDocument",
                side_effect=error,
            ):
                encrypted_result = service.understand(
                    "task_pdf_errors", encrypted["asset_id"]
                )
            self.assertEqual(encrypted_result["status"], "FAILED")
            self.assertEqual(
                encrypted_result["warnings"][0]["code"], "PDF_PASSWORD_PROTECTED"
            )

    @staticmethod
    def _import(
        assets: AssetService,
        task_id: str,
        filename: str,
        content: bytes,
        key: str,
    ) -> dict:
        return assets.import_bytes(
            task_id,
            filename=filename,
            content=content,
            description="test input",
            source="unit_test",
            idempotency_key=key,
        )

    @staticmethod
    def _image_bytes(format_name: str) -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (160, 90), "blue").save(output, format=format_name)
        return output.getvalue()

    @staticmethod
    def _image_pdf(page_count: int) -> bytes:
        output = io.BytesIO()
        first = Image.new("RGB", (160, 90), "white")
        rest = [Image.new("RGB", (160, 90), "gray") for _ in range(page_count - 1)]
        first.save(output, format="PDF", save_all=True, append_images=rest)
        return output.getvalue()

    @staticmethod
    def _text_pdf(text: str) -> bytes:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
            ),
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        document = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for index, body in enumerate(objects, start=1):
            offsets.append(len(document))
            document.extend(f"{index} 0 obj\n".encode())
            document.extend(body)
            document.extend(b"\nendobj\n")
        xref = len(document)
        document.extend(f"xref\n0 {len(objects) + 1}\n".encode())
        document.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            document.extend(f"{offset:010d} 00000 n \n".encode())
        document.extend(
            (
                f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n"
            ).encode()
        )
        return bytes(document)


if __name__ == "__main__":
    unittest.main()
