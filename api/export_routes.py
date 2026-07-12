"""
api/export_routes.py -- the HTTP surface for exporting a conversation or
its underlying data. Ported from main.py's /export/preview and
/export/download, adapted to pull data from the session store instead
of an in-memory dict.
"""

from datetime import date
from typing import List, Literal, Optional
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import io

from memory.session_store import get_conversation_history, get_latest_query_result
from tools.export_builders import (
    build_csv, build_xlsx, build_pdf, build_pptx,
    generate_export_summary, _safe_filename,
)

router = APIRouter()


class ExportPreviewRequest(BaseModel):
    conversation_id: str
    title: str = "Revenue Intelligence Report"
    export_type: Literal["pdf", "pptx", "csv", "xlsx"] = "pdf"


class ExportPreviewResponse(BaseModel):
    content: Optional[str] = None
    title: str
    export_type: str
    total_rows: int = 0
    generated_at: str


@router.post("/export/preview", response_model=ExportPreviewResponse)
def export_preview(req: ExportPreviewRequest):
    """
    For csv/xlsx: no AI content is generated -- the preview is just a
    confirmation that real data is available, since those formats are
    raw data exports, not narrative documents.
    For pdf/pptx: generates the actual executive summary, so the person
    can see it before committing to a download.
    """
    stored = get_latest_query_result(req.conversation_id)

    if req.export_type in ("csv", "xlsx"):
        if not stored or not stored.get("rows"):
            raise HTTPException(status_code=404, detail="No dataset found for this conversation. Ask a data question first, then export.")
        return ExportPreviewResponse(
            content=None,
            title=req.title,
            export_type=req.export_type,
            total_rows=stored["total_rows"],
            generated_at=date.today().isoformat(),
        )

    history = get_conversation_history(req.conversation_id)
    if not history:
        raise HTTPException(status_code=400, detail="No conversation to export.")

    content = generate_export_summary(
        conversation=history,
        title=req.title,
        export_type=req.export_type,
        columns=stored["columns"] if stored else None,
        rows=stored["rows"] if stored else None,
    )
    return ExportPreviewResponse(
        content=content,
        title=req.title,
        export_type=req.export_type,
        total_rows=stored["total_rows"] if stored else 0,
        generated_at=date.today().isoformat(),
    )


class ExportDownloadRequest(BaseModel):
    conversation_id: str
    format: Literal["pdf", "pptx", "csv", "xlsx"]
    content: Optional[str] = None  # required for pdf/pptx -- the previewed content
    title: str = "Revenue Intelligence Report"


@router.post("/export/download")
def export_download(req: ExportDownloadRequest):
    if req.format in ("csv", "xlsx"):
        stored = get_latest_query_result(req.conversation_id)
        if not stored or not stored.get("rows"):
            raise HTTPException(status_code=404, detail="No dataset found for this conversation.")

        if req.format == "csv":
            file_bytes = build_csv(stored["columns"], stored["rows"])
            media_type = "text/csv"
            ext = "csv"
        else:
            file_bytes = build_xlsx(stored["columns"], stored["rows"], req.title)
            media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ext = "xlsx"

        return StreamingResponse(
            io.BytesIO(file_bytes),
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{_safe_filename(req.title)}.{ext}"',
                "X-Total-Rows": str(stored["total_rows"]),
            },
        )

    if not req.content:
        raise HTTPException(status_code=400, detail="content is required for PDF/PPTX export -- call /export/preview first.")

    try:
        if req.format == "pdf":
            file_bytes = build_pdf(req.title, req.content)
            media_type = "application/pdf"
            ext = "pdf"
        else:
            file_bytes = build_pptx(req.title, req.content)
            media_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ext = "pptx"

        return StreamingResponse(
            io.BytesIO(file_bytes),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{_safe_filename(req.title)}.{ext}"'},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"File generation error: {exc}")
