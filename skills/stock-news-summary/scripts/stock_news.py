#!/usr/bin/env python3
"""Original, dependency-free watchlist news collector. MIT, HW3 project.

Google News RSS is a best-effort public feed, not a guaranteed API. Returned
links may be Google redirect links; publisher URLs are identified separately.
Summaries must be explicitly based on headlines, not unseen article bodies.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

COMPANIES = {
    "NVDA": ["NVIDIA", "NVDA"], "TSLA": ["Tesla", "TSLA"],
    "GOOGL": ["Alphabet", "Google", "GOOGL", "GOOG"], "AAPL": ["Apple", "AAPL"],
    "MSFT": ["Microsoft", "MSFT"], "AMZN": ["Amazon", "AMZN"],
    "META": ["Meta", "META", "Facebook"], "AMD": ["Advanced Micro Devices", "AMD"],
    "AVGO": ["Broadcom", "AVGO"], "ORCL": ["Oracle", "ORCL"],
}


def public_url(value: str) -> str:
    if not isinstance(value, str) or len(value)>2048:raise ValueError("Invalid public URL")
    parsed=urlparse(value)
    host=parsed.hostname or ""
    if parsed.scheme not in {"https","http"} or parsed.username or parsed.password or "." not in host:
        raise ValueError("Only public web links are accepted")
    if host.lower().endswith((".localhost",".local",".internal")) or host.lower()=="localhost":
        raise ValueError("Private web link")
    try:
        if not ipaddress.ip_address(host).is_global:raise ValueError("Private IP link")
    except ValueError as error:
        if re.fullmatch(r"[0-9.:]+",host):raise ValueError("Invalid/private IP link") from error
    query=[(k,v) for k,v in parse_qsl(parsed.query,keep_blank_values=True) if not k.lower().startswith("utm_") and k.lower() not in {"gclid","fbclid"}]
    return urlunparse((parsed.scheme,parsed.netloc.lower(),parsed.path,"",urlencode(query),""))


def xml_root(raw: bytes):
    if len(raw)>2_000_000 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("Feed exceeds bounds or contains a DTD/entity")
    return ET.fromstring(raw)


def feed_fetch(query: str) -> bytes:
    url="https://news.google.com/rss/search?"+urlencode({"q":query,"hl":"en-US","gl":"US","ceid":"US:en"})
    with urlopen(Request(url,headers={"User-Agent":"HW3-Stock-News/0.1"}),timeout=18) as response:
        return response.read(2_000_001)


def normalized_title(title: str) -> str:
    return " ".join(re.findall(r"\w+",unicodedata.normalize("NFKC",title).casefold()))


def parse_items(raw: bytes, ticker: str, aliases: list[str], since: datetime, now: datetime) -> tuple[list,dict]:
    stats={"stale":0,"invalid_date":0,"future":0,"irrelevant":0,"invalid_link":0}
    rows=[]
    for item in xml_root(raw).findall("./channel/item")[:200]:
        title=(item.findtext("title") or "").strip()[:400]
        publisher=(item.findtext("source") or "Unknown publisher").strip()[:120]
        if title.endswith(" - "+publisher):title=title[:-(len(publisher)+3)]
        try:
            published=parsedate_to_datetime(item.findtext("pubDate") or "")
            if published.tzinfo is None:raise ValueError("No date timezone")
            published=published.astimezone(timezone.utc)
        except (ValueError,TypeError,OverflowError):stats["invalid_date"]+=1;continue
        if published<since:stats["stale"]+=1;continue
        if published>now+timedelta(minutes=5):stats["future"]+=1;continue
        if not any(re.search(r"(?<!\w)"+re.escape(a)+r"(?!\w)",title,re.I) for a in aliases):
            stats["irrelevant"]+=1;continue
        try:link=public_url(item.findtext("link") or "")
        except ValueError:stats["invalid_link"]+=1;continue
        source=item.find("source")
        source_url=source.get("url","") if source is not None else ""
        try:source_url=public_url(source_url)
        except ValueError:source_url=""
        rows.append({"headline":title,"publisher":publisher,"url":link,"publisher_url":source_url,
                     "published_at_utc":published.isoformat(),"ticker":ticker,
                     "headline_only":True,"untrusted_content":True})
    return rows,stats


def collect(tickers: list[str], hours: int=48, per_company: int=3, *, company_map: dict | None=None,
            fetch=feed_fetch, now: datetime | None=None) -> dict:
    if not isinstance(tickers,list) or not 1<=len(tickers)<=20 or len(set(tickers))!=len(tickers) or any(not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}",t) for t in tickers):
        raise ValueError("Supply 1–20 unique uppercase tickers")
    if type(hours) is not int or not 1<=hours<=168 or type(per_company) is not int or not 1<=per_company<=5:
        raise ValueError("Hours 1–168; per-company limit 1–5")
    mapping={**COMPANIES,**(company_map or {})}
    for key,aliases in mapping.items():
        if not isinstance(aliases,list) or not 1<=len(aliases)<=10 or any(not isinstance(a,str) or not 1<=len(a)<=80 or any(c in a for c in '"\r\n()') for a in aliases):
            raise ValueError("Company aliases must be bounded plain strings")
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:raise ValueError("A timezone-aware reference time is required")
    now=now.astimezone(timezone.utc);since=now-timedelta(hours=hours)
    def one(ticker):
        aliases=mapping.get(ticker,[ticker])
        query="("+" OR ".join('"'+a+'"' for a in aliases[:3])+") (stock OR earnings OR technology) when:"+str(max(1,(hours+23)//24))+"d"
        try:
            rows,stats=parse_items(fetch(query),ticker,aliases,since,now)
            return ticker,rows,stats,None
        except Exception as error:
            return ticker,[],{},type(error).__name__
    groups,stories,keys,urls,failures,duplicate_count=[],{}, {},{},[],0
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(one,tickers))
    for ticker,rows,stats,error in results:
        refs=[]
        for row in sorted(rows,key=lambda r:r["published_at_utc"],reverse=True):
            key=normalized_title(row["headline"])
            existing=keys.get(key) or urls.get(row["url"])
            if existing:
                duplicate_count+=1;story_id=existing
            else:
                story_id=hashlib.sha256((key+row["url"]).encode()).hexdigest()[:12]
                keys[key]=urls[row["url"]]=story_id
                stories[story_id]={**row,"id":story_id,"tickers":[]}
            if ticker not in stories[story_id]["tickers"]:stories[story_id]["tickers"].append(ticker)
            if story_id not in refs and len(refs)<per_company:refs.append(story_id)
        status="source_unavailable" if error else "ok" if refs else "no_recent_relevant_news"
        groups.append({"ticker":ticker,"story_ids":refs,"status":status,"filtered":stats})
        if error:failures.append({"ticker":ticker,"error_type":error})
    included={r for group in groups for r in group["story_ids"]}
    return {"retrieved_at_utc":now.isoformat(),"window_start_utc":since.isoformat(),"lookback_hours":hours,
        "watchlist":tickers,"groups":groups,"stories":[s for k,s in stories.items() if k in included],
        "duplicate_candidates_removed":duplicate_count,"source_failures":failures,
        "missing_tickers":[g["ticker"] for g in groups if not g["story_ids"]],
        "basis":"Public RSS headlines only; article bodies were not read. Links may redirect through Google News.",
        "untrusted_content":True,"investment_actions_available":False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watchlist",default=",".join(COMPANIES))
    parser.add_argument("--hours",type=int,default=48)
    parser.add_argument("--per-company",type=int,default=3)
    parser.add_argument("--company-map",help="Optional JSON mapping ticker to company-name aliases")
    args=parser.parse_args()
    mapping=None
    if args.company_map:
        with open(args.company_map) as stream:mapping=json.load(stream)
    report=collect([t.strip().upper() for t in args.watchlist.split(",")],args.hours,args.per_company,company_map=mapping)
    print(json.dumps(report,indent=2,ensure_ascii=False))
    return 0 if report["stories"] else 1


if __name__=="__main__":raise SystemExit(main())
