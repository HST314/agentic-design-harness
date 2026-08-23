"""Deterministic document parsing with targeted VLM visual understanding."""

from __future__ import annotations

import io
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, NoReturn, cast

import pypdfium2 as pdfium
from PIL import Image, UnidentifiedImageError

from ..core.errors import HarnessError
from ..storage.atomic import atomic_write_json, read_json
from ..storage.layout import validate_identifier
from ..storage.repository import utc_now
from .assets import AssetService
from .model_clients import ModelClientFactory, ModelClientFailure, ModelResult
from .task_config import TaskConfigService
from .usage import UsageService

PARSER_VERSION = "pdfium-5.13-v1"
_TEXT_MIME = {"text/plain", "text/markdown"}
_IMAGE_MIME = {"image/jpeg", "image/png", "image/webp"}
_SUPPORTED_MIME = _TEXT_MIME | _IMAGE_MIME | {"application/pdf"}
_WHITESPACE = re.compile(r"\s+")
_MAX_IMAGE_PIXELS = 25_000_000
_VISUAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "blocks", "warnings"],
    "properties": {
        "summary": {"type": "string", "maxLength": 4000},
        "blocks": {
            "type": "array",
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "text", "bbox", "confidence"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["paragraph", "table", "image_description"],
                    },
                    "text": {"type": "string", "minLength": 1, "maxLength": 20000},
                    "bbox": {
                        "oneOf": [
                            {"type": "null"},
                            {
                                "type": "array",
                                "items": {"type": "number", "minimum": 0, "maximum": 1},
                                "minItems": 4,
                                "maxItems": 4,
                            },
                        ]
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
        "warnings": {
            "type": "array",
            "maxItems": 100,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
    },
}


class _AssetContentError(ValueError):
    def __init__(self, code: str, message: str, *, page: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.page = page


class AssetUnderstandingService:
    """Create cacheable, source-addressable understanding records per asset."""

    def __init__(
        self,
        assets: AssetService,
        task_config: TaskConfigService,
        model_clients: ModelClientFactory,
        usage: UsageService,
    ) -> None:
        self.assets = assets
        self.task_config = task_config
        self.model_clients = model_clients
        self.usage = usage

    def prepare(self, task_id: str, asset_ids: list[str]) -> list[dict[str, Any]]:
        snapshot = self.task_config.resolve(task_id)
        limits = snapshot.runtime.document_processing
        if len(asset_ids) > limits.max_files_per_task:
            raise HarnessError(
                "ASSET_VALIDATION_FAILED", "The task contains too many files to analyze."
            )
        manifests = [self.assets.verify_asset(task_id, asset_id) for asset_id in asset_ids]
        if sum(item["size_bytes"] for item in manifests) > limits.max_total_bytes:
            raise HarnessError(
                "ASSET_VALIDATION_FAILED", "The task files exceed the analysis size limit."
            )
        return [self.understand(task_id, item["asset_id"]) for item in manifests]

    def understand(self, task_id: str, asset_id: str) -> dict[str, Any]:
        validate_identifier(asset_id, "asset_id")
        manifest = self.assets.verify_asset(task_id, asset_id)
        if manifest["mime_type"] not in _SUPPORTED_MIME:
            raise HarnessError(
                "ASSET_VALIDATION_FAILED",
                "This asset type is not supported by the understanding pipeline.",
                {"asset_id": asset_id},
            )
        public_config = self.task_config.get_public(task_id)
        path = self._path(task_id, asset_id)
        if path.exists():
            cached = read_json(path)
            if (
                cached.get("source_sha256") == manifest["sha256"]
                and cached.get("parser_version") == PARSER_VERSION
                and cached.get("model_config_hash") == public_config["config_hash"]
            ):
                self.assets.store.contracts.validate("asset-understanding", cached)
                return deepcopy(cached)
        snapshot = self.task_config.resolve(task_id)
        try:
            with self.assets.open_verified_asset(task_id, asset_id) as opened:
                source = opened.stream.read()
            if manifest["mime_type"] in _TEXT_MIME:
                page_count, summary, blocks, warnings = self._understand_text(
                    source,
                    markdown=manifest["mime_type"] == "text/markdown",
                    chunk_chars=snapshot.runtime.document_processing.text_chunk_chars,
                )
            elif manifest["mime_type"] in _IMAGE_MIME:
                page_count, summary, blocks, warnings = self._understand_image(
                    task_id, asset_id, source, manifest["mime_type"]
                )
            else:
                page_count, summary, blocks, warnings = self._understand_pdf(
                    task_id, asset_id, source
                )
            status = "READY"
        except _AssetContentError as exc:
            page_count, summary, blocks = None, "", []
            warnings = [{"code": exc.code, "message": exc.message, "page": exc.page}]
            status = "FAILED"
        document = {
            "schema_version": "1.0",
            "asset_id": asset_id,
            "source_sha256": manifest["sha256"],
            "status": status,
            "media_type": manifest["mime_type"],
            "page_count": page_count,
            "summary": summary,
            "blocks": blocks,
            "warnings": warnings,
            "parser_version": PARSER_VERSION,
            "model_config_hash": public_config["config_hash"],
            "created_at": utc_now(),
        }
        self.assets.store.contracts.validate("asset-understanding", document)
        atomic_write_json(path, document, mode=0o640)
        return deepcopy(document)

    def inspect_region(
        self,
        task_id: str,
        asset_id: str,
        *,
        page: int | None,
        bbox: list[float],
        question: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        normalized_bbox = cast(list[float], self._bbox(bbox))
        normalized_question = question.strip()
        if not normalized_question or len(normalized_question) > 4000:
            raise HarnessError("VALIDATION_ERROR", "The visual inspection question is invalid.")
        manifest = self.assets.verify_asset(task_id, asset_id)
        with self.assets.open_verified_asset(task_id, asset_id) as opened:
            source = opened.stream.read()
        if manifest["mime_type"] in _IMAGE_MIME:
            if page not in {None, 1}:
                raise HarnessError("VALIDATION_ERROR", "Image assets only have page 1.")
            image = self._load_image(source)
            citation_page = 1
        elif manifest["mime_type"] == "application/pdf":
            if page is None or page < 1:
                raise HarnessError("VALIDATION_ERROR", "A PDF inspection requires a page.")
            document = self._open_pdf(source)
            try:
                if page > len(document):
                    raise HarnessError("VALIDATION_ERROR", "The PDF page is out of range.")
                pdf_page = document[page - 1]
                try:
                    image = self._render_page(pdf_page)
                finally:
                    pdf_page.close()
            finally:
                document.close()
            citation_page = page
        else:
            raise HarnessError(
                "VALIDATION_ERROR", "Visual region inspection requires an image or PDF."
            )
        cropped = self._crop(image, normalized_bbox)
        result = self._inspect(
            task_id,
            cropped,
            prompt=normalized_question,
            idempotency_key=idempotency_key,
        )
        output = self._validate_visual_output(result)
        return {
            "asset_id": asset_id,
            "page": citation_page,
            "bbox": normalized_bbox,
            "summary": output["summary"],
            "blocks": output["blocks"],
            "warnings": output["warnings"],
        }

    def _understand_text(
        self, source: bytes, *, markdown: bool, chunk_chars: int
    ) -> tuple[None, str, list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            text = source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _AssetContentError(
                "TEXT_NOT_UTF8", "该文本资料不是有效 UTF-8, 请转换编码后重新上传。"
            ) from exc
        blocks = self._text_blocks(text, chunk_chars, page=None, markdown=markdown)
        summary = self._summary(blocks)
        warnings = [] if blocks else [
            {"code": "EMPTY_DOCUMENT", "message": "资料中没有可读取的文字。", "page": None}
        ]
        return None, summary, blocks, warnings

    def _understand_image(
        self, task_id: str, asset_id: str, source: bytes, media_type: str
    ) -> tuple[int, str, list[dict[str, Any]], list[dict[str, Any]]]:
        image = self._load_image(source)
        snapshot = self.task_config.resolve(task_id)
        if snapshot.runtime.document_processing.visual_analysis == "never":
            return 1, f"图像尺寸 {image.width}x{image.height}。", [], [
                {
                    "code": "VISUAL_ANALYSIS_DISABLED",
                    "message": "视觉理解已由部署配置禁用。",
                    "page": 1,
                }
            ]
        result = self._inspect(
            task_id,
            image,
            prompt="描述图像中的文字、版式、主体、颜色和需要遵守的设计约束。",
            idempotency_key=f"understand-{asset_id}-image",
        )
        output = self._validate_visual_output(result)
        return (
            1,
            output["summary"],
            self._visual_blocks(output["blocks"], page=1, prefix="p1_v"),
            self._visual_warnings(output["warnings"], page=1),
        )

    def _understand_pdf(
        self, task_id: str, asset_id: str, source: bytes
    ) -> tuple[int, str, list[dict[str, Any]], list[dict[str, Any]]]:
        document = self._open_pdf(source)
        blocks: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        snapshot = self.task_config.resolve(task_id)
        settings = snapshot.runtime.document_processing
        try:
            page_count = len(document)
            if page_count < 1:
                self._content_error("PDF_EMPTY", "PDF 没有可读取的页面。")
            if page_count > settings.max_pdf_pages:
                self._content_error(
                    "PDF_PAGE_LIMIT",
                    f"PDF 共 {page_count} 页, 超过 {settings.max_pdf_pages} 页限制。",
                )
            for page_index in range(page_count):
                page_number = page_index + 1
                page = document[page_index]
                try:
                    text_page = page.get_textpage()
                    try:
                        text = text_page.get_text_range()
                    finally:
                        text_page.close()
                    page_blocks = self._text_blocks(
                        text,
                        settings.text_chunk_chars,
                        page=page_number,
                        markdown=False,
                    )
                    blocks.extend(page_blocks)
                    has_image = any(
                        page.get_objects(filter=(pdfium.raw.FPDF_PAGEOBJ_IMAGE,))
                    )
                    needs_visual = settings.visual_analysis == "always" or (
                        settings.visual_analysis == "auto"
                        and (has_image or len(_WHITESPACE.sub("", text)) < 40)
                    )
                    if needs_visual:
                        image = self._render_page(page)
                        result = self._inspect(
                            task_id,
                            image,
                            prompt=(
                                "描述这一页的文字、图表、图片、版式与设计约束; "
                                "只补充页面视觉信息, 不重复明显的正文。"
                            ),
                            idempotency_key=f"understand-{asset_id}-page-{page_number}",
                        )
                        output = self._validate_visual_output(result)
                        blocks.extend(
                            self._visual_blocks(
                                output["blocks"], page=page_number, prefix=f"p{page_number}_v"
                            )
                        )
                        warnings.extend(
                            self._visual_warnings(output["warnings"], page=page_number)
                        )
                    elif not page_blocks:
                        warnings.append(
                            {
                                "code": "PAGE_WITHOUT_TEXT",
                                "message": "该页没有可提取文字。",
                                "page": page_number,
                            }
                        )
                finally:
                    page.close()
        finally:
            document.close()
        return page_count, self._summary(blocks), blocks, warnings

    def _inspect(
        self, task_id: str, image: Image.Image, *, prompt: str, idempotency_key: str
    ) -> ModelResult:
        snapshot = self.task_config.resolve(task_id)
        client = self.model_clients.vision(
            snapshot,
            snapshot.runtime.models.vision_understanding,
            timeout_seconds=snapshot.runtime.master.model_timeout_seconds,
        )
        encoded = io.BytesIO()
        prepared = image.convert("RGB")
        prepared.save(encoded, format="JPEG", quality=88, optimize=True)
        try:
            result = client.inspect_image(
                image_bytes=encoded.getvalue(),
                media_type="image/jpeg",
                prompt=prompt,
                response_schema=_VISUAL_SCHEMA,
                idempotency_key=idempotency_key,
            )
        except ModelClientFailure:
            raise
        self.usage.record_master_model_call(task_id, result)
        return result

    @staticmethod
    def _validate_visual_output(result: ModelResult) -> dict[str, Any]:
        output = result.output
        if output is None or set(output) != {"summary", "blocks", "warnings"}:
            raise ModelClientFailure(
                "MODEL_OUTPUT_INVALID", "Vision model returned an invalid result."
            )
        summary = output["summary"]
        blocks = output["blocks"]
        warnings = output["warnings"]
        if (
            not isinstance(summary, str)
            or len(summary) > 4000
            or not isinstance(blocks, list)
            or len(blocks) > 100
            or not isinstance(warnings, list)
            or len(warnings) > 100
            or not all(isinstance(item, str) and 0 < len(item) <= 1000 for item in warnings)
        ):
            raise ModelClientFailure(
                "MODEL_OUTPUT_INVALID", "Vision model returned malformed fields."
            )
        for block in blocks:
            if not isinstance(block, dict) or set(block) != {
                "kind",
                "text",
                "bbox",
                "confidence",
            }:
                raise ModelClientFailure(
                    "MODEL_OUTPUT_INVALID", "Vision model returned a malformed block."
                )
            if block["kind"] not in {"paragraph", "table", "image_description"}:
                raise ModelClientFailure(
                    "MODEL_OUTPUT_INVALID", "Vision model returned an invalid block kind."
                )
            if (
                not isinstance(block["text"], str)
                or not block["text"]
                or len(block["text"]) > 20000
            ):
                raise ModelClientFailure(
                    "MODEL_OUTPUT_INVALID", "Vision model returned invalid block text."
                )
            try:
                AssetUnderstandingService._bbox(block["bbox"], allow_none=True)
            except HarnessError as exc:
                raise ModelClientFailure(
                    "MODEL_OUTPUT_INVALID", "Vision model returned an invalid bounding box."
                ) from exc
            if type(block["confidence"]) not in {int, float} or not (
                0 <= block["confidence"] <= 1
            ):
                raise ModelClientFailure(
                    "MODEL_OUTPUT_INVALID", "Vision model returned invalid confidence."
                )
        return cast(dict[str, Any], deepcopy(output))

    @staticmethod
    def _text_blocks(
        text: str, chunk_chars: int, *, page: int | None, markdown: bool
    ) -> list[dict[str, Any]]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []
        sections = [item.strip() for item in re.split(r"\n\s*\n", normalized) if item.strip()]
        chunks: list[tuple[str, str]] = []
        for section in sections:
            kind = "heading" if markdown and section.lstrip().startswith("#") else "paragraph"
            start = 0
            while start < len(section):
                end = min(start + chunk_chars, len(section))
                if end < len(section):
                    boundary = section.rfind("\n", start, end)
                    if boundary <= start:
                        boundary = section.rfind(" ", start, end)
                    if boundary > start:
                        end = boundary
                chunk = section[start:end].strip()
                if chunk:
                    chunks.append((kind, chunk))
                start = max(end, start + 1)
        page_prefix = "text" if page is None else f"p{page}"
        return [
            {
                "block_id": f"{page_prefix}_b{index}",
                "page": page,
                "kind": kind,
                "text": chunk,
                "bbox": None,
                "extraction_method": "utf8" if page is None else "embedded_text",
                "confidence": 1.0,
            }
            for index, (kind, chunk) in enumerate(chunks, start=1)
        ]

    @staticmethod
    def _visual_blocks(
        blocks: list[dict[str, Any]], *, page: int, prefix: str
    ) -> list[dict[str, Any]]:
        return [
            {
                "block_id": f"{prefix}{index}",
                "page": page,
                "kind": item["kind"],
                "text": item["text"],
                "bbox": item["bbox"],
                "extraction_method": "vlm",
                "confidence": float(item["confidence"]),
            }
            for index, item in enumerate(blocks, start=1)
        ]

    @staticmethod
    def _visual_warnings(warnings: list[str], *, page: int) -> list[dict[str, Any]]:
        return [
            {"code": "VLM_WARNING", "message": message, "page": page}
            for message in warnings
        ]

    @staticmethod
    def _summary(blocks: list[dict[str, Any]]) -> str:
        joined = " ".join(_WHITESPACE.sub(" ", item["text"]).strip() for item in blocks)
        return joined[:4000]

    @staticmethod
    def _load_image(source: bytes) -> Image.Image:
        try:
            with Image.open(io.BytesIO(source)) as opened:
                opened.verify()
            with Image.open(io.BytesIO(source)) as reopened:
                if reopened.width * reopened.height > _MAX_IMAGE_PIXELS:
                    raise _AssetContentError(
                        "IMAGE_PIXEL_LIMIT",
                        "该图片解压后的像素数量超过安全限制, 请缩小尺寸后上传。",
                    )
                image = reopened.convert("RGB")
        except _AssetContentError:
            raise
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
            raise _AssetContentError(
                "IMAGE_INVALID", "该图片已损坏或无法读取, 请重新导出后上传。"
            ) from exc
        if image.width < 1 or image.height < 1:
            raise _AssetContentError("IMAGE_INVALID", "该图片没有有效尺寸。")
        image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
        return image

    @staticmethod
    def _open_pdf(source: bytes) -> pdfium.PdfDocument:
        try:
            return pdfium.PdfDocument(source)
        except pdfium.PdfiumError as exc:
            if getattr(exc, "err_code", None) == pdfium.raw.FPDF_ERR_PASSWORD:
                raise _AssetContentError(
                    "PDF_PASSWORD_PROTECTED", "该 PDF 已加密, 请上传未加密版本。"
                ) from exc
            raise _AssetContentError(
                "PDF_INVALID", "该 PDF 已损坏或无法读取, 请重新导出后上传。"
            ) from exc

    @staticmethod
    def _render_page(page: pdfium.PdfPage) -> Image.Image:
        width, height = page.get_size()
        if width <= 0 or height <= 0:
            raise _AssetContentError("PDF_PAGE_INVALID", "PDF 页面尺寸无效。")
        scale = min(2.0, 2048 / max(width, height))
        bitmap = cast(Any, page).render(scale=scale)
        try:
            return bitmap.to_pil().convert("RGB").copy()
        finally:
            bitmap.close()

    @staticmethod
    def _crop(image: Image.Image, bbox: list[float]) -> Image.Image:
        left, top, right, bottom = bbox
        pixels = (
            round(left * image.width),
            round(top * image.height),
            round(right * image.width),
            round(bottom * image.height),
        )
        return image.crop(pixels)

    @staticmethod
    def _bbox(value: Any, *, allow_none: bool = False) -> list[float] | None:
        if value is None and allow_none:
            return None
        if (
            not isinstance(value, list)
            or len(value) != 4
            or not all(type(item) in {int, float} and 0 <= item <= 1 for item in value)
        ):
            raise HarnessError("VALIDATION_ERROR", "The inspection bounding box is invalid.")
        normalized = [float(item) for item in value]
        if normalized[0] >= normalized[2] or normalized[1] >= normalized[3]:
            raise HarnessError("VALIDATION_ERROR", "The inspection bounding box is empty.")
        return normalized

    def _path(self, task_id: str, asset_id: str) -> Path:
        path = (
            self.assets.store.layout.workspace_root
            / "tasks"
            / task_id
            / "inputs"
            / "understanding"
        )
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path / f"{asset_id}.json"

    @staticmethod
    def _content_error(code: str, message: str) -> NoReturn:
        raise _AssetContentError(code, message)
