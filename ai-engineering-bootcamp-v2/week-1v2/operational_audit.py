"""Persist API response/usage records without buffering streams or headers.

Transport errors and unavailable accounting remain explicit. Request IDs link
retrieval evidence to the eventual HTTP outcome. Credentials are never collected.
"""
import json
import time
from uuid import uuid4
from starlette.concurrency import run_in_threadpool
from operational_store import record_event


class OperationalAuditMiddleware:
    def __init__(self, app):
        self.app=app

    async def __call__(self, scope, receive, send):
        paths={'/ask','/ask/stream','/summarize','/analyze-sentiment','/ingest','/ingest/batch','/ingest-pdf','/debug/retrieve'}
        if scope['type'] != 'http' or scope.get('path') not in paths:
            return await self.app(scope,receive,send)
        request_id=str(uuid4())
        scope.setdefault('state',{})['operational_request_id']=request_id
        started=time.perf_counter()
        data=bytearray()
        status=None
        is_json=False
        truncated=False
        error=None
        # Record request start before running work: disconnected/failed requests
        # remain visible even if a final event cannot be written.
        await run_in_threadpool(record_event,'http_started',{'request_id':request_id,'path':scope['path'],'method':scope['method']})
        async def capture(message):
            nonlocal status,is_json,truncated
            if message['type']=='http.response.start':
                status=message['status']
                is_json=any(k.lower()==b'content-type' and b'application/json' in v for k,v in message.get('headers',[]))
            elif message['type']=='http.response.body' and is_json:
                body=message.get('body',b'')
                if len(data)+len(body)<=2_000_000: data.extend(body)
                else: truncated=True
            await send(message)
        try:
            await self.app(scope,receive,capture)
        except BaseException as exc:
            error=type(exc).__name__
            raise
        finally:
            response=None
            if is_json and data and not truncated:
                try: response=json.loads(data)
                except (ValueError,UnicodeDecodeError): error=error or 'InvalidJSONResponse'
            payload={'request_id':request_id,'path':scope['path'],'http_status':status,
                'latency_ms':(time.perf_counter()-started)*1000,'response':response,
                'response_truncated':truncated,'error':error,
                'usage_known':isinstance(response,dict) and response.get('tokens_used') is not None}
            await run_in_threadpool(record_event,'http_completed',payload,event_id=request_id)
