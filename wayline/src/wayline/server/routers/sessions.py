from __future__ import annotations

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from wayline.server.db import get_conn
from wayline.server.session_models import SessionChunk, SessionInvocation, SessionResult
from wayline.server import session_service as service

router = APIRouter(tags=['agent sessions'])


@router.post('/agent-sessions')
def register(body: SessionInvocation):
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return service.register(conn, body)


@router.get('/agent-sessions')
def sessions(project_id: str | None = None, workflow_id: str | None = None,
             intent_id: str | None = None, fact_id: str | None = None,
             offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
    with get_conn() as conn:
        return service.list_sessions(conn, project_id=project_id, workflow_id=workflow_id,
                                     intent_id=intent_id, fact_id=fact_id, offset=offset, limit=limit)


@router.get('/agent-sessions/{invocation_id}')
def detail(invocation_id: UUID):
    with get_conn() as conn:
        return service.session_detail(conn, str(invocation_id))


@router.post('/agent-sessions/{invocation_id}/chunks')
def chunks(invocation_id: UUID, body: SessionChunk):
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return service.append_chunk(conn, str(invocation_id), body)


@router.post('/agent-sessions/{invocation_id}/event-chunks')
def event_chunks(invocation_id: UUID, body: SessionChunk):
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return service.append_chunk(conn, str(invocation_id), body, events=True)


@router.post('/agent-conversations/{conversation_id}/results')
def results(conversation_id: UUID, body: SessionResult):
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return service.link_result(conn, str(conversation_id), body)


@router.get('/agent-sessions/{invocation_id}/events')
def events(invocation_id: UUID, after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    with get_conn() as conn:
        return service.read_events(conn, str(invocation_id), after, limit)


@router.get('/agent-sessions/{invocation_id}/stream')
async def stream(invocation_id: UUID, request: Request, after: int = Query(0, ge=0)):
    identifier = str(invocation_id)
    with get_conn() as conn:
        service.session_detail(conn, identifier)

    async def changes():
        cursor, previous, idle = after, None, 0
        while not await request.is_disconnected():
            with get_conn() as conn:
                metadata = service.session_detail(conn, identifier)
                records = service.read_events(conn, identifier, cursor, 50)
            fingerprint = json.dumps(metadata, sort_keys=True)
            if previous != fingerprint or records['items']:
                cursor = records['next_after']
                yield 'event: update\ndata: '+json.dumps({'session': metadata, 'events': records['items'], 'next_after': cursor}, ensure_ascii=True)+'\n\n'
                previous, idle = fingerprint, 0
            elif idle >= 30:
                yield ': heartbeat\n\n'
                idle = 0
            idle += 1
            await asyncio.sleep(0 if records['has_more'] else .5)
    return StreamingResponse(changes(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
