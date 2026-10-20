"""현재 LangSmith 프로젝트의 요청 추적을 읽는 관리자 API."""
import logging
import os
import secrets
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from langsmith import Client
from langsmith.utils import LangSmithError, LangSmithNotFoundError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(response: Response, x_admin_token: Annotated[str | None, Header()] = None):
    response.headers["Cache-Control"] = "no-store"
    expected = os.getenv("ADMIN_API_TOKEN", "")
    if not expected:
        raise HTTPException(503, "관리자 접근 키가 설정되지 않았습니다.")
    if not x_admin_token or not secrets.compare_digest(x_admin_token.encode(), expected.encode()):
        raise HTTPException(401, "관리자 접근 키를 확인해주세요.")


def get_client() -> Client:
    key = os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY", "")
    if not key or "..." in key:
        raise HTTPException(503, "LangSmith API 키를 설정해주세요.")
    return Client(api_key=key, api_url=os.getenv("LANGSMITH_ENDPOINT") or
                  os.getenv("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com"),
                  timeout_ms=10000)


def project_name() -> str:
    return os.getenv("LANGSMITH_PROJECT") or os.getenv("LANGCHAIN_PROJECT", "capstone-all-total")


def duration_ms(run):
    if run.end_time is None:
        return None
    return round((run.end_time - run.start_time).total_seconds() * 1000, 1)


def run_summary(run):
    outputs = run.outputs or {}
    metadata = outputs.get("metadata", {})
    return {
        "id": str(run.id), "name": run.name,
        "query": str((run.inputs or {}).get("message", ""))[:500],
        "start_time": run.start_time.isoformat(), "duration_ms": duration_ms(run),
        "status": "error" if run.error else "running" if run.end_time is None else "completed",
        "tokens": metadata.get("token_count"), "estimated_cost_usd": metadata.get("cost_usd"),
        "backend": metadata.get("llm_backend"), "route": metadata.get("route"),
        "validation_passed": metadata.get("validation_passed"),
        "validation_attempts": metadata.get("validation_attempts"),
    }


@router.get("/runs", dependencies=[Depends(require_admin)])
def list_requests(limit: Annotated[int, Query(ge=1, le=50)] = 20,
                  client: Client = Depends(get_client)):
    project = project_name()
    try:
        runs = list(client.list_runs(
            project_name=project, is_root=True, limit=limit,
            filter='or(eq(name, "chat_request"), eq(name, "chat_request_stream"))'))
    except LangSmithNotFoundError:
        runs = []  # 첫 추적이 전송되기 전에는 프로젝트가 없을 수 있다.
    except LangSmithError:
        logger.exception("LangSmith request list failed")
        raise HTTPException(502, "LangSmith 기록을 가져오지 못했습니다. 연결과 API 키를 확인해주세요.")
    summaries = [run_summary(run) for run in runs]
    return {"project": project, "tracing_enabled": (
        os.getenv("LANGSMITH_TRACING") or os.getenv("LANGCHAIN_TRACING_V2", "false")
    ).lower() == "true", "runs": summaries}


@router.get("/runs/{run_id}", dependencies=[Depends(require_admin)])
def request_detail(run_id: UUID, client: Client = Depends(get_client)):
    try:
        project = client.read_project(project_name=project_name())
        run = client.read_run(run_id)
        if (str(run.session_id) != str(project.id) or run.parent_run_id is not None
                or run.name not in ("chat_request", "chat_request_stream")):
            raise HTTPException(404, "해당 프로젝트의 요청을 찾을 수 없습니다.")
        # 프로젝트와 루트 요청 소속을 확인한 뒤에만 하위 실행을 가져온다.
        run = client.read_run(run_id, load_child_runs=True)
    except LangSmithNotFoundError:
        raise HTTPException(404, "요청 기록을 찾을 수 없습니다.")
    except LangSmithError:
        logger.exception("LangSmith request detail failed")
        raise HTTPException(502, "요청 기록을 가져오지 못했습니다. 잠시 후 다시 시도해주세요.")
    nodes = []
    pending = [(run, 0)]
    while pending and len(nodes) < 200:
        node, depth = pending.pop()
        nodes.append({"id": str(node.id), "name": node.name, "type": node.run_type,
                      "depth": depth, "duration_ms": duration_ms(node), "error": bool(node.error)})
        pending.extend((child, depth + 1) for child in reversed(node.child_runs or []))
    outputs = run.outputs or {}
    return {**run_summary(run), "reply": outputs.get("reply"),
            "prompt": outputs.get("last_prompt"), "events": outputs.get("events", []),
            "nodes": nodes, "nodes_truncated": bool(pending)}
