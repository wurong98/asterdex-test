#!/usr/bin/env python3
"""Public endpoint measurements; never sends orders or uses credentials."""
import argparse
import asyncio
import json
import math
import platform
import re
import socket
import ssl
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

TARGETS = {
    'binance': ('https://fapi.binance.com/fapi/v1/time',
                'wss://fstream.binance.com/market/ws/{symbol}@aggTrade'),
    'aster': ('https://fapi.asterdex.com/fapi/v3/time',
              'wss://fstream.asterdex.com/ws/{symbol}@aggTrade'),
}


def stats(values):
    if not values:
        return {'n': 0}
    v = sorted(values)
    return {'n': len(v), 'min_ms': round(v[0], 3),
            **{f'p{p}_ms': round(v[max(0, math.ceil(len(v)*p/100)-1)], 3)
               for p in (50, 95, 99)}, 'max_ms': round(v[-1], 3)}


def clock_status():
    try:
        p = subprocess.run(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'],
                           capture_output=True, text=True, timeout=3)
        return p.stdout.strip() if p.returncode == 0 else 'unknown'
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'


async def dns(host):
    start = time.perf_counter()
    try:
        info = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, 443, type=socket.SOCK_STREAM), 8)
        return {'host': host, 'ips': sorted({x[4][0] for x in info}),
                'lookup_ms': (time.perf_counter()-start)*1000}
    except Exception as e:
        return {'host': host, 'error': str(e)}


async def host_probe(host, name, emit):
    resolution = await dns(host)
    emit({'kind': 'dns', 'exchange': name, **resolution})
    print(f"{host}: DNS {resolution.get('ips', resolution.get('error'))}", flush=True)
    attempts = []
    for i in range(3):
        row = {'kind': 'host_https', 'exchange': name, 'host': host, 'sample': i}
        writer = None
        stage = 'tcp'
        try:
            start = time.perf_counter()
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, 443), 8)
            row['tcp_with_dns_ms'] = (time.perf_counter()-start)*1000
            row['remote_ip'] = writer.get_extra_info('peername')[0]
            stage = 'tls'
            tls_start = time.perf_counter()
            await asyncio.wait_for(writer.start_tls(ssl.create_default_context(), server_hostname=host), 8)
            row['tls_handshake_ms'] = (time.perf_counter()-tls_start)*1000
            stage = 'http'
            writer.write(f'GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\nUser-Agent: region-probe/1\r\n\r\n'.encode())
            await writer.drain()
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 8)
            lines = header.decode('iso-8859-1').split('\r\n')
            row['http_status_line'] = lines[0]
            row['headers_total_ms'] = (time.perf_counter()-start)*1000
            row['headers'] = {k.strip().lower(): v.strip() for line in lines[1:] if ':' in line
                              for k, v in [line.split(':', 1)]
                              if k.strip().lower() in ('server', 'via', 'x-amz-cf-pop', 'cf-ray')}
            row['transport_ok'] = True
            print(f"{host}: {row['remote_ip']} {lines[0]} TCP含DNS={row['tcp_with_dns_ms']:.1f}ms TLS={row['tls_handshake_ms']:.1f}ms", flush=True)
        except Exception as e:
            row.update(transport_ok=False, failed_stage=stage, error=repr(e))
            print(f'{host}: {stage} 失败 {e!r}', flush=True)
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 2)
                except Exception:
                    pass
        emit(row)
        attempts.append(row)
        if row.get('http_status_line', '').split(' ')[1:2] in (['403'], ['418'], ['429'], ['451']):
            break
    return {'dns': resolution, 'https_attempts': attempts}


async def rest(name, url, args, emit):
    rows, offsets, failures = [], [], 0
    async with aiohttp.ClientSession(trust_env=False,
            timeout=aiohttp.ClientTimeout(total=8)) as session:
        for i in range(args.samples):
            start, wall = time.perf_counter(), time.time()*1000
            row = {'kind': 'rest', 'exchange': name, 'sample': i,
                   'first_request': i == 0, 'url': url}
            try:
                async with session.get(url, allow_redirects=False) as r:
                    body = await r.text()
                    elapsed = (time.perf_counter()-start)*1000
                    row.update(status=r.status, rtt_ms=elapsed,
                               headers={k: v for k, v in r.headers.items()
                                        if k.lower() in ('server', 'via', 'x-amz-cf-pop', 'cf-ray')})
                    try:
                        server_time = json.loads(body).get('serverTime')
                    except (ValueError, AttributeError):
                        server_time = None
                    if r.status == 200 and isinstance(server_time, (int, float)):
                        row['valid'] = True
                        row['clock_offset_estimate_ms'] = server_time - (wall+elapsed/2)
                        offsets.append((elapsed, row['clock_offset_estimate_ms']))
                        if i > 0:
                            rows.append(elapsed)
                    else:
                        failures += 1
                        row.update(valid=False, body_preview=body[:300])
                    emit(row)
                    if r.status in (403, 418, 429, 451):
                        emit({'kind': 'rest_stopped', 'exchange': name,
                              'reason': f'HTTP {r.status}; no repeated blocked/rate-limited requests'})
                        break
            except Exception as e:
                failures += 1
                emit({**row, 'valid': False, 'error': repr(e)})
            await asyncio.sleep(args.interval)
    return {'warm_rest_rtt': stats(rows), 'rest_failures': failures,
            'clock_offset_estimate_ms': min(offsets)[1] if offsets else None}


async def websocket(name, url, args, offset, emit):
    ages, raw_ages, gaps, handshakes, ping_rtts, first_events = [], [], [], [], [], []
    count, disconnects, attempts = 0, 0, 0
    deadline = time.monotonic()+args.duration
    async with aiohttp.ClientSession(trust_env=False,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=8)) as session:
        while time.monotonic() < deadline:
            attempts += 1
            last = None
            started = time.perf_counter()
            try:
                async with session.ws_connect(url, autoping=False,
                                              timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                    handshake = (time.perf_counter()-started)*1000
                    handshakes.append(handshake)
                    emit({'kind': 'ws_connected', 'exchange': name,
                          'url': url, 'handshake_ms': handshake,
                          'remote_peer': ws.get_extra_info('peername')})
                    next_ping, pending, first = time.monotonic(), {}, True
                    while time.monotonic() < deadline:
                        now_mono = time.monotonic()
                        if now_mono >= next_ping:
                            token = str(time.perf_counter_ns()).encode()
                            pending = {token: time.perf_counter()}
                            await ws.ping(token)
                            next_ping = now_mono+5
                        try:
                            msg = await asyncio.wait_for(ws.receive(), max(0.001, min(next_ping-time.monotonic(), deadline-time.monotonic())))
                        except asyncio.TimeoutError:
                            if time.monotonic() >= deadline:
                                break
                            continue
                        now, wall = time.perf_counter(), time.time()*1000
                        if msg.type == aiohttp.WSMsgType.PING:
                            await ws.pong(msg.data)
                            continue
                        if msg.type == aiohttp.WSMsgType.PONG:
                            sent = pending.pop(bytes(msg.data), None)
                            if sent is not None:
                                rtt = (now-sent)*1000
                                ping_rtts.append(rtt)
                                emit({'kind': 'ws_ping_rtt', 'exchange': name, 'rtt_ms': rtt})
                            continue
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                data = data.get('data', data)
                                if data.get('e') != 'aggTrade':
                                    continue
                                event = data.get('E')
                            except (ValueError, AttributeError):
                                continue
                            if first:
                                first_events.append((now-started)*1000)
                                first = False
                            count += 1
                            row = {'kind': 'ws_event', 'exchange': name, 'received_ms': wall,
                                   'event_ms': event, 'trade_id': data.get('a')}
                            if last is not None:
                                gap = (now-last)*1000
                                gaps.append(gap)
                                row['interarrival_ms'] = gap
                            last = now
                            if isinstance(event, (int, float)):
                                row['raw_event_age_ms'] = wall-event
                                raw_ages.append(wall-event)
                                if offset is not None:
                                    age = wall+offset-event
                                    ages.append(age)
                                    row['estimated_event_age_ms'] = age
                            emit(row)
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                          aiohttp.WSMsgType.ERROR):
                            raise ConnectionError(f'websocket closed: {ws.close_code}, {ws.exception()}')
            except Exception as e:
                disconnects += 1
                emit({'kind': 'ws_error', 'exchange': name, 'error': repr(e)})
                if isinstance(e, aiohttp.WSServerHandshakeError) and e.status in (403, 418, 429, 451):
                    break
                await asyncio.sleep(min(3, max(0, deadline-time.monotonic())))
    return {'ws_messages': count, 'ws_data_received': count > 0, 'ws_attempts': attempts, 'ws_errors': disconnects,
            'ws_handshake': stats(handshakes), 'ws_ping_rtt': stats(ping_rtts),
            'raw_event_age': stats(raw_ages), 'first_event_since_connect_start': stats(first_events),
            'ws_interarrival': stats(gaps),
            'estimated_event_age': stats(ages)}


async def main(args):
    folder = Path(args.output)/f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{args.label}"
    folder.mkdir(parents=True, exist_ok=False)
    with (folder/'raw.jsonl').open('w') as raw:
        def emit(row):
            raw.write(json.dumps({'utc': datetime.now(timezone.utc).isoformat(), **row})+'\n')
            raw.flush()
        metadata = {'label': args.label, 'utc': datetime.now(timezone.utc).isoformat(),
                    'platform': platform.platform(), 'ntp_synchronized': clock_status(),
                    'parameters': vars(args), 'proxy': 'disabled',
                    'limitations': 'Public endpoints only. Not order latency or engine location. '
                    'Event age uses uncertain midpoint clock estimate; interarrival includes market activity.'}
        emit({'kind': 'metadata', **metadata})
        async def venue(name):
            url, ws_template = TARGETS[name]
            ws_template = getattr(args, name+'_ws_url') or ws_template
            hosts = (url.split('/')[2], ws_template.split('/')[2])
            probes = dict(zip(hosts, await asyncio.gather(*(host_probe(h, name, emit) for h in hosts))))
            result = {'warm_rest_rtt': stats([]), 'rest_failures': 0, 'clock_offset_estimate_ms': None}
            if args.rest:
                print(f'{name}: REST 测试中...', flush=True)
                result = await rest(name, url, args, emit)
            result['hosts'] = probes
            print(f'{name}: WebSocket 测试 {args.duration}s...', flush=True)
            result.update(await websocket(name, ws_template.format(symbol=args.symbol.lower()),
                                          args, result['clock_offset_estimate_ms'], emit))
            return name, result
        results = dict(await asyncio.gather(*(venue(n) for n in TARGETS)))
    summary = {'metadata': metadata, 'results': results}
    (folder/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f'结果目录: {folder}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', default=re.sub(r'[^a-zA-Z0-9_-]', '-', socket.gethostname()) or 'local',
                        help='云商和区域标签，默认本机主机名，例如 aws-tokyo-az1')
    parser.add_argument('--duration', type=int, default=300, help='每个交易所 WS 采样秒数')
    parser.add_argument('--rest', action='store_true', help='额外测试 REST；默认只做域名传输诊断和 WS 实测')
    parser.add_argument('--binance-ws-url', help='自定义币安公开 aggTrade WSS 地址，可含 {symbol}')
    parser.add_argument('--aster-ws-url', help='自定义 Aster 公开 aggTrade WSS 地址，可含 {symbol}')
    parser.add_argument('--samples', type=int, default=60, help='REST 请求次数（含首个冷请求）')
    parser.add_argument('--interval', type=float, default=1, help='REST 请求间隔秒数')
    parser.add_argument('--symbol', default='BTCUSDT')
    parser.add_argument('--output', default='results')
    args = parser.parse_args()
    if args.duration < 1 or args.samples < 2 or args.interval < 0.2:
        parser.error('duration >= 1, samples >= 2, interval >= 0.2')
    if not args.label or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in args.label):
        parser.error('label 只允许字母、数字、连字符和下划线')
    for value in (args.binance_ws_url, args.aster_ws_url):
        if value and (not value.startswith('wss://') or '@' in value.split('/')[2]):
            parser.error('自定义地址必须是无凭据的 wss:// 公开行情入口')
    asyncio.run(main(args))
