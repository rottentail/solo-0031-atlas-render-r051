#!/usr/bin/env python3
"""Capture real-call evidence for the client reconnection contract.

Runs five scenarios against a temporary Atlas Render server and prints one
JSON evidence report. docs/client-contract.md quotes this output as the
review basis for the reconnection guarantees:

E1 连接中断后用同一幂等键重放，找回同一个任务；同键不同语义返回 409。
E2 同一快照换用新键会重复出图：内容相同、队列与结果字节翻倍。
E3 场景修改后，已接受任务仍承诺冻结时的图；新图只能来自新快照。
E4 结果清理后下载返回 410，任务事实保留；同快照新任务确定性再生同内容。
E5 快照撤回后新键提交返回 409；收据重放与重试仍兑现撤回前的承诺。
"""
import hashlib
import http.client
import json
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from walkthrough import Demo, sample  # noqa: E402


def drop_post(url, path, body, key):
    """Send a job submission and close the connection before reading the response."""
    parts = urllib.parse.urlparse(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=15)
    conn.request('POST', path, body=json.dumps(body),
                 headers={'Content-Type': 'application/json', 'Idempotency-Key': key})
    conn.close()


def heavy():
    d = sample()
    d['width'] = 1500
    d['height'] = 1500
    d['layers'] = [dict(d['layers'][0], id=f'sheet-{i}', width=1500, height=1500,
                        transform={'scaleX': 1, 'scaleY': 1}) for i in range(16)]
    return d


def run(demo):
    ev = {}
    doc = sample()
    scene = demo.call('PUT', '/v1/scenes/emblem', {'name': '断连契约', 'expectedRevision': 0, 'document': doc})
    snap = demo.call('POST', '/v1/scenes/emblem/snapshots', {'expectedRevision': scene['revision']}, 201)

    # E1: 提交后连接中断（响应丢失），重连用同键同体重放。
    drop_post(demo.url, '/v1/jobs', {'snapshotId': snap['id'], 'scale': 1}, 'evidence-reconnect')
    time.sleep(.3)
    replay = demo.call('POST', '/v1/jobs', {'snapshotId': snap['id'], 'scale': 1}, 202,
                       {'Idempotency-Key': 'evidence-reconnect'})
    conflict = demo.call('POST', '/v1/jobs', {'snapshotId': snap['id'], 'scale': 2}, 409,
                         {'Idempotency-Key': 'evidence-reconnect'})
    ev['E1_reconnect_replay'] = {
        'jobAfterReconnect': replay['id'],
        'sameKeyDifferentScale': {'status': 409, 'body': conflict},
    }

    # E2: 同一快照同一缩放换用新键，产生第二个任务和第二份相同像素。
    dup = demo.call('POST', '/v1/jobs', {'snapshotId': snap['id'], 'scale': 1}, 202,
                    {'Idempotency-Key': 'evidence-duplicate'})
    first = demo.wait(replay['id'])
    second = demo.wait(dup['id'])
    png1 = demo.call('GET', '/v1/artifacts/' + first['artifactId'])
    png2 = demo.call('GET', '/v1/artifacts/' + second['artifactId'])
    cmp12 = demo.call('POST', '/v1/compare', {'left': first['artifactId'], 'right': second['artifactId']})
    status = demo.call('GET', '/v1/status')
    queued = sorted({e['subject'] for e in demo.call('GET', '/v1/events') if e['kind'] == 'job.queued'})
    ev['E2_new_key_duplicate'] = {
        'firstJob': first['id'], 'secondJob': second['id'],
        'distinctJobsForSameContent': first['id'] != second['id'],
        'queuedJobsSoFar': queued,
        'artifactDigestsEqual': hashlib.sha256(png1).hexdigest() == hashlib.sha256(png2).hexdigest(),
        'compareChangedPixels': cmp12['changedPixels'],
        'singleArtifactBytes': len(png1),
        'artifactBytesTotal': status['artifactBytes'],
    }

    # E3: 用户修改场景（修订前进），旧任务仍承诺冻结时的图。
    doc['background'] = '#000000'
    scene2 = demo.call('PUT', '/v1/scenes/emblem',
                       {'name': '断连契约', 'expectedRevision': scene['revision'], 'document': doc})
    snap_after_edit = demo.call('GET', '/v1/snapshots/' + snap['id'])
    job1 = demo.call('GET', '/v1/jobs/' + first['id'])
    snap2 = demo.call('POST', '/v1/scenes/emblem/snapshots', {'expectedRevision': scene2['revision']}, 201)
    third = demo.wait(demo.call('POST', '/v1/jobs', {'snapshotId': snap2['id'], 'scale': 1}, 202,
                                {'Idempotency-Key': 'evidence-revised'})['id'])
    png3 = demo.call('GET', '/v1/artifacts/' + third['artifactId'])
    cmp13 = demo.call('POST', '/v1/compare', {'left': first['artifactId'], 'right': third['artifactId']})
    ev['E3_scene_edited'] = {
        'sceneRevisionNow': scene2['revision'],
        'snapshotDigestBeforeEdit': snap['digest'],
        'snapshotDigestAfterEdit': snap_after_edit['digest'],
        'snapshotUnchangedByEdit': snap['digest'] == snap_after_edit['digest'],
        'oldJobStillPinsSnapshot': job1['snapshotId'] == snap['id'],
        'oldJobState': job1['state'],
        'revisedSnapshot': snap2['id'],
        'oldVsRevisedChangedPixels': cmp13['changedPixels'],
        'oldVsRevisedDigestsDiffer':
            hashlib.sha256(png1).hexdigest() != hashlib.sha256(png3).hexdigest(),
    }

    # E4: 清理结果字节；成功任务不可重试；同快照新任务确定性再生同内容。
    demo.call('DELETE', '/v1/artifacts/' + third['artifactId'])
    gone = demo.call('GET', '/v1/artifacts/' + third['artifactId'], status=410)
    job3 = demo.call('GET', '/v1/jobs/' + third['id'])
    retry_succeeded = demo.call('POST', '/v1/jobs/' + third['id'] + '/retry', status=409)
    fourth = demo.wait(demo.call('POST', '/v1/jobs', {'snapshotId': snap2['id'], 'scale': 1}, 202,
                                 {'Idempotency-Key': 'evidence-regenerate'})['id'])
    png4 = demo.call('GET', '/v1/artifacts/' + fourth['artifactId'])
    ev['E4_purge_regenerate'] = {
        'downloadAfterPurge': {'status': 410, 'body': gone},
        'jobStateAfterPurge': job3['state'],
        'retrySucceededJob': {'status': 409, 'body': retry_succeeded},
        'regeneratedJob': fourth['id'],
        'regeneratedArtifactIsNewId': fourth['artifactId'] != third['artifactId'],
        'regeneratedDigestEqualsPurged':
            hashlib.sha256(png4).hexdigest() == hashlib.sha256(png3).hexdigest(),
    }

    # E5: 撤回快照；新键被 409 拒绝；收据重放与重试仍兑现撤回前的固定内容。
    scene3 = demo.call('PUT', '/v1/scenes/emblem',
                       {'name': '断连契约', 'expectedRevision': scene2['revision'], 'document': heavy()})
    snap3 = demo.call('POST', '/v1/scenes/emblem/snapshots', {'expectedRevision': scene3['revision']}, 201)
    before = demo.wait(demo.call('POST', '/v1/jobs', {'snapshotId': snap3['id'], 'scale': 1}, 202,
                                 {'Idempotency-Key': 'evidence-before-revoke'})['id'])
    j5 = demo.call('POST', '/v1/jobs', {'snapshotId': snap3['id'], 'scale': 1}, 202,
                   {'Idempotency-Key': 'evidence-revoke'})
    demo.wait(j5['id'], wanted=('running',))
    cancelled = demo.call('POST', '/v1/jobs/' + j5['id'] + '/cancel')
    demo.call('POST', '/v1/snapshots/' + snap3['id'] + '/revoke')
    blocked = demo.call('POST', '/v1/jobs', {'snapshotId': snap3['id'], 'scale': 1}, 409,
                        {'Idempotency-Key': 'evidence-after-revoke'})
    replayed = demo.call('POST', '/v1/jobs', {'snapshotId': snap3['id'], 'scale': 1}, 202,
                         {'Idempotency-Key': 'evidence-revoke'})
    snap3_state = demo.call('GET', '/v1/snapshots/' + snap3['id'])
    retried = demo.call('POST', '/v1/jobs/' + j5['id'] + '/retry', status=202)
    final = demo.wait(j5['id'])
    cmp56 = demo.call('POST', '/v1/compare', {'left': before['artifactId'], 'right': final['artifactId']})
    events = [e['kind'] for e in demo.call('GET', '/v1/events') if e['subject'] in (j5['id'], snap3['id'])]
    ev['E5_snapshot_revoked'] = {
        'cancelledBeforeRevoke': cancelled['state'],
        'newKeyAfterRevoke': {'status': 409, 'body': blocked},
        'receiptReplayAfterRevoke': {'status': 202, 'job': replayed['id'], 'state': replayed['state']},
        'receiptReplayFindsSameJob': replayed['id'] == j5['id'],
        'revokedSnapshotStillReadable': {'state': snap3_state['state'], 'width': snap3_state['document']['width']},
        'retryAfterRevoke': {'status': 202, 'attempt': retried['attempt']},
        'finalState': final['state'],
        'retriedContentEqualsPreRevokeRender': cmp56['changedPixels'] == 0,
        'eventTrail': events,
    }
    return ev


def main():
    with tempfile.TemporaryDirectory(prefix='atlas-reconnect-') as temp:
        directory = Path(temp)
        binary = directory / 'atlas'
        subprocess.run(['go', 'build', '-o', str(binary), './cmd/atlas'], cwd=ROOT, check=True, timeout=60)
        demo = Demo(binary, directory)
        try:
            print(json.dumps(run(demo), ensure_ascii=False, indent=2))
        finally:
            demo.stop()
            demo.log.close()


if __name__ == '__main__':
    main()
