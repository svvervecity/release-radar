#!/usr/bin/env python3
"""Daily MusicBrainz full-catalog/date-window scan, joined to an exported artist watchlist.

The scan covers all artists in the watchlist when records appear in the queried
MusicBrainz release-group window, but is NOT a guarantee of complete release discovery.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
WATCHLIST = ROOT / 'artist_watchlist.json'
OUT = ROOT / 'releases.json'
USER_AGENT = f"PersonalReleaseRadar/1.0 ({os.environ.get('MUSICBRAINZ_CONTACT', 'noncommercial research; GitHub Actions')})"
BASE = 'https://musicbrainz.org/ws/2/release-group/'
MAX_PAGES_PER_DAY = 8
RATE_LIMIT_SEC = 1.3

def norm(s: str) -> str:
    s = unicodedata.normalize('NFKC', s).casefold()
    s = ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', s).strip()

def valid_date(s):
    return bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', s or ''))

def get_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent':USER_AGENT, 'Accept':'application/json'})
            with urllib.request.urlopen(req, timeout=35) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f'Fetch failed (attempt {attempt+1}/{retries}): {exc}', file=sys.stderr)
            if attempt + 1 == retries: raise
            time.sleep(2 ** attempt * 3)

def scan_days(days, fetch=get_json, wait=time.sleep, max_pages=MAX_PAGES_PER_DAY):
    """Return (release groups, attempted count, failed day names, truncated day names)."""
    rows, failed, truncated = [], [], []
    attempts = 0
    for date in days:
        offset = 0
        full_count = None
        while True:
            q = f'firstreleasedate:[{date} TO {date}]'
            url = BASE + '?' + urllib.parse.urlencode({'query':q, 'fmt':'json', 'limit':100, 'offset':offset})
            if attempts: wait(RATE_LIMIT_SEC)
            attempts += 1
            try:
                payload = fetch(url)
            except Exception as exc:
                failed.append(str(date)); print('Missing date:',date,exc,file=sys.stderr);break
            group = payload.get('release-groups', [])
            full_count = int(payload.get('count', 0))
            rows.extend(group)
            offset += len(group)
            if not group or offset >= full_count:
                break
            if offset >= max_pages*100:
                truncated.append(str(date));break
    return rows, attempts, failed, truncated

def match_release(item, by_id, by_name):
    credits = item.get('artist-credit', [])
    identities = []
    if not isinstance(credits, list): credits = []
    for c in credits:
        if not isinstance(c, dict):continue
        a = c.get('artist') or {}
        identities.append((a.get('id',''), c.get('name') or a.get('name','')))
    for artistid, artistname in identities:
        if artistid and artistid in by_id:
            return by_id[artistid], 'MusicBrainz artist ID'
    for artistid, artistname in identities:
        hit = by_name.get(norm(artistname))
        # Reject misleading same-name hits when stored MBID(s) conflict.
        if hit and (not artistid or not hit.get('musicbrainz_artist_ids') or artistid in hit['musicbrainz_artist_ids']):
            return hit,'Exact artist name' if not artistid else 'MusicBrainz exact name'
    return None, None

def build_new_release(item, artist, match, today):
    date = item.get('first-release-date','')
    if not valid_date(date): return None
    title = item.get('title') or ''
    kind = (item.get('primary-type') or 'Other').capitalize()
    name = artist['artist']
    artist_ids = artist.get('musicbrainz_artist_ids') or []
    rgid = item.get('id') or ''
    if not title or not rgid: return None
    return {'id':'mb-'+rgid,'artist':name,'title':title,'type':kind,
        'date':date,'status':'released' if date <= today.isoformat() else 'upcoming',
        'tier':artist['priority_tier'],'plays':artist['listen_plays'],
        'ratings':artist['rated_albums'],'average_rating':artist.get('rated_avg'),
        'source':'MusicBrainz', 'source_url':f'https://musicbrainz.org/release-group/{rgid}',
        'sources':[{'name':'MusicBrainz','url':f'https://musicbrainz.org/release-group/{rgid}'}],
        'match':match,'mbid':rgid,'first_seen':today.isoformat(),
        'notes':'Metadata-based match; verify regional availability at the source.','flag':''}

def merge(seed, fresh):
    """Preserve verified editorial records, append catalog discoveries, attach catalogue source where applicable."""
    idx = {(norm(x['artist']),norm(x['title'])):x for x in seed}
    for item in fresh:
        key=(norm(item['artist']),norm(item['title']))
        old=idx.get(key)
        if not old:
            idx[key]=item; continue
        if item['source_url'] not in {x['url'] for x in old['sources']}:
            old['sources'].append({'name':'MusicBrainz','url':item['source_url']})
        old['mbid']=item.get('mbid') or old.get('mbid','')
        if old['date'] != item['date']:
            old['notes'] = (old.get('notes','') + ' / Catalog date differs: ' + item['date']).strip(' /')
    return list(idx.values())

def run(today=None, fetch=None, wait=time.sleep):
    today=today or dt.datetime.now(ZoneInfo("America/Toronto")).date()
    watch=json.loads(WATCHLIST.read_text())
    old=json.loads(OUT.read_text()) if OUT.exists() else {'releases':[],'metadata':{}}
    artists=watch['artists']; by_id={mbid:a for a in artists for mbid in a.get('musicbrainz_artist_ids',[])}
    by_name={norm(a['artist']):a for a in artists}
    start=today-dt.timedelta(days=today.weekday()); end=start+dt.timedelta(days=6)
    # Scan from Monday of the current week through 35 days ahead.
    days=[start+dt.timedelta(days=i) for i in range((today+dt.timedelta(days=35)-start).days+1)]
    groups,attempts,failed,truncated = scan_days(days, fetch=fetch or get_json,wait=wait)
    fresh=[]
    for item in groups:
        artist, mode=match_release(item,by_id,by_name)
        if not artist:continue
        candidate=build_new_release(item,artist,mode,today)
        if candidate: fresh.append(candidate)
    # Only display releases this week + 35 days forward. Keep editorial older results if week still current.
    low,high=start.isoformat(),(today+dt.timedelta(days=35)).isoformat()
    legacy=[x for x in old.get('releases',[]) if low <= x.get('date','') <= high]
    entries=merge(legacy,fresh)
    entries.sort(key=lambda r:(r['status']!='released',r['tier'],r['date'] if r['status']=='upcoming' else ''.join(chr(255-ord(c)) for c in r['date']),-r['plays'],r['artist'].casefold()))
    meta={
        'updated_at':dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
        'week_start':low,'week_end':end.isoformat(),
        'artist_watchlist':len(artists),
        'listened_or_rated':sum(1 for a in artists if a.get('listen_plays') or a.get('rated_albums')),
        'source':'Editorially verified week-of-Oct-23 seed + rolling MusicBrainz release-group catalog scan',
        'coverage':f'Scanned {len(days)-len(set(failed))-len(set(truncated))}/{len(days)} date windows; source catalog is not exhaustive. Missing or delayed metadata, obscure releases and some reissues may be missed.',
        'scan_complete':not (failed or truncated), 'automated':True,
        'queried_days':len(days),'api_requests':attempts,'records_scanned':len(groups),'catalog_matches':len(fresh),
        'failed_days':sorted(set(failed)),'truncated_days':sorted(set(truncated)),
        'source_links':old.get('metadata',{}).get('source_links',[])
    }
    # Do not overwrite previously verified data on a complete network failure.
    if not groups and failed:
        meta['coverage']='Refresh failed; showing last successfully saved findings. '+meta['coverage']
        entries=legacy
    payload={'metadata':meta,'releases':entries}
    target=OUT.with_suffix('.json.tmp');target.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n');target.replace(OUT)
    print(f'Wrote {len(entries)} records, {len(fresh)} matches, {attempts} API requests, {len(failed)} failed dates, {len(truncated)} truncated dates')
    return payload

if __name__ == '__main__':
    run()