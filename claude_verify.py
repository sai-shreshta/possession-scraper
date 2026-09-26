"""Second pass: Claude reviews the rows the scraper wasn't sure about and decides the possession date."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import anthropic

import extractor as ex

log = logging.getLogger("scraper")

# USD per 1M tokens (input, output). Web search is billed separately per search.
PRICES = {
    "claude-fable-5-1": (10, 50), "claude-opus-5-5": (4, 20), "claude-opus-5": (5, 25),
    "claude-opus-4-8": (5, 25), "claude-sonnet-5": (2, 10), "claude-sonnet-4-6": (3, 15),
    "claude-haiku-4-5": (1, 5),
}
WEB_SEARCH_USD = 10 / 1000
FALLBACK_MODELS = ("claude-opus-5", "claude-opus-5-5", "claude-fable-5-1")

SYSTEM = """You verify possession dates of Indian residential buildings and housing projects for a property database.

For each building you get its name, locality, city and coordinates, plus evidence a web scraper collected: search-result snippets and date mentions it extracted, each with its source URL.

Decide the possession date of THIS building at THIS location:
- "Possession date" means when the builder hands over / handed over flats (listing sites say "Possession by", "Possession from", "Ready to move since", "Completed in"). For old completed buildings a year of construction or completion is fine.
- Watch for evidence that is about something else: same-name projects in other localities or cities, "similar projects" carousels on listing pages, launch dates, RERA registration validity dates, and article publish dates.
- A RERA "proposed/revised completion date" is a legal deadline that is often later than the real possession date. Prefer listing-site possession dates; fall back to the RERA date only when nothing better exists, and say so.
- If the project has phases or towers with different dates, give the date of the earliest phase that most sources refer to and describe the split in phase_note.
- Sources disagreeing by a few months is normal; pick the date most independent sources support.
- Do not guess. If the evidence does not clearly refer to this building, return an empty possession_date with confidence "none".
{web}
Finish by calling record_verdict exactly once."""

WEB_NOTE = ("- You can use web_search (at most {n} searches) when the evidence is missing, thin, or conflicting. "
            "Search for the project name with its locality and city plus words like \"possession\", "
            "\"ready to move\" or \"RERA\". Skip searching when the evidence is already clear.")

VERDICT_TOOL = {
    "name": "record_verdict",
    "description": "Record the final possession-date verdict for this building. Call it exactly once, at the end.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "possession_date": {"type": "string",
                                "description": "YYYY-MM, or YYYY if only the year is known, or empty string if undetermined"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low", "none"]},
            "status": {"type": "string", "enum": ["Ready to Move", "Under Construction", "Unknown"]},
            "phase_note": {"type": "string", "description": "Phase/tower split if relevant, else empty"},
            "source_url": {"type": "string", "description": "Best supporting URL, else empty"},
            "reasoning": {"type": "string", "description": "One or two sentences"},
        },
        "required": ["possession_date", "confidence", "status", "phase_note", "source_url", "reasoning"],
        "additionalProperties": False,
    },
}


def build_prompt(r: dict, pack: dict) -> str:
    scraped = r["scraped"]
    lines = [f"Building: {r['name']}", f"Locality: {r['loc'] or '-'}", f"City: {r['city'] or '-'}"]
    if r.get("address") and r["address"] != r["loc"]:
        lines.append(f"Full address: {r['address']}")
    if r.get("rera"):
        lines.append(f"RERA ID: {r['rera']}")
    if r.get("lat") and r.get("lon"):
        lines.append(f"Coordinates: {r['lat']}, {r['lon']}")
    if scraped.get("found"):
        lines.append(f"\nScraper's guess: {scraped.get('display')} ({scraped.get('label')} confidence, "
                     f"{scraped.get('sources')} sites agreeing). Other dates it saw: {scraped.get('alternatives') or 'none'}")
    else:
        lines.append("\nScraper found no date.")
    dates = pack.get("dates") or []
    if dates:
        lines.append("\nDate mentions extracted from pages/snippets (date | type | source | text):")
        for d in dates:
            lines.append(f"- {d['date']} | {d['kind']} | {d['url']} | \"{d['text']}\"")
    snips = pack.get("snippets") or []
    if snips:
        lines.append("\nSearch results:")
        for s in snips:
            lines.append(f"- {s['title']} | {s['url']}\n  {s['snippet']}")
    if not dates and not snips:
        lines.append("\nNo evidence was collected for this building.")
    return "\n".join(lines)


@dataclass
class Spend:
    budget: float | None
    usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    searches: int = 0
    rows: int = 0

    def add(self, resp, requested_model: str, discount: float = 1.0):
        u = resp.usage
        pin, pout = PRICES.get(resp.model, PRICES.get(requested_model, (5, 25)))
        pin, pout = pin * discount, pout * discount
        inp = (u.input_tokens or 0) + (u.cache_creation_input_tokens or 0) * 1.25 + (u.cache_read_input_tokens or 0) * 0.1
        stu = getattr(u, "server_tool_use", None)
        searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        self.input_tokens += u.input_tokens or 0
        self.output_tokens += u.output_tokens or 0
        self.searches += searches
        self.usd += inp * pin / 1e6 + (u.output_tokens or 0) * pout / 1e6 + searches * WEB_SEARCH_USD

    def over(self) -> bool:
        return self.budget is not None and self.usd >= self.budget

    def __str__(self):
        return (f"${self.usd:.2f} spent over {self.rows} rows (in {self.input_tokens:,} / out {self.output_tokens:,} "
                f"tokens, {self.searches} web searches)")


class Verifier:
    def __init__(self, model: str, effort: str, web_searches: int, spend: Spend):
        self.client = anthropic.AsyncAnthropic(max_retries=5)
        self.model, self.effort, self.web, self.spend = model, effort, web_searches, spend
        self.system = SYSTEM.format(web=WEB_NOTE.format(n=web_searches) if web_searches else "")
        self.tools = [VERDICT_TOOL]
        if web_searches:
            wtype = "web_search_20250305" if "haiku" in model else "web_search_20260209"
            self.tools.append({"type": wtype, "name": "web_search", "max_uses": web_searches,
                               "user_location": {"type": "approximate", "country": "IN"}})

    def base_params(self, messages) -> dict:
        haiku = "haiku" in self.model
        kw = dict(model=self.model, max_tokens=4096 if haiku else 16000, system=self.system, tools=self.tools,
                  messages=messages)
        if haiku:
            if not self.web:
                # No thinking on Haiku, so the verdict tool can be forced: one turn, no nudging.
                kw["tool_choice"] = {"type": "tool", "name": "record_verdict"}
        else:
            kw["output_config"] = {"effort": self.effort}
        return kw

    def _request(self, messages) -> dict:
        kw = self.base_params(messages)
        if self.model in FALLBACK_MODELS:
            kw["betas"] = ["server-side-fallback-2026-07-01"]
            kw["fallbacks"] = "default"
        return kw

    async def verify(self, prompt: str) -> dict:
        messages = [{"role": "user", "content": prompt}]
        for _ in range(5):
            resp = await self.client.beta.messages.create(**self._request(messages))
            self.spend.add(resp, self.model)
            if resp.stop_reason == "refusal":
                return {"possession_date": "", "confidence": "none", "status": "Unknown", "phase_note": "",
                        "source_url": "", "reasoning": "Claude declined this request"}
            for b in resp.content:
                if b.type == "tool_use" and b.name == "record_verdict":
                    return dict(b.input)
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "pause_turn":
                messages.append({"role": "user", "content": "Now call record_verdict with your final answer."})
        raise RuntimeError("no verdict after 5 turns")


def to_row(v: dict, model: str) -> dict:
    iso = (v.get("possession_date") or "").strip()
    display = ""
    if len(iso) == 7 and iso[4] == "-" and iso[:4].isdigit() and iso[5:].isdigit():
        display = ex._display(int(iso[:4]), int(iso[5:]), "month")
    elif len(iso) == 4 and iso.isdigit():
        display = iso
    else:
        iso = ""
    return {"iso": iso, "display": display, "confidence": v.get("confidence", ""), "status": v.get("status", ""),
            "phase_note": v.get("phase_note", ""), "source_url": v.get("source_url", ""),
            "reasoning": v.get("reasoning", ""), "model": model}


def select_rows(results: dict[int, dict], scope: str) -> list[int]:
    out = []
    for i, d in sorted(results.items()):
        if d.get("note") == "empty building name":
            continue
        label = d.get("label", "")
        if scope == "all" or (scope == "unsure" and label != "High") or (scope == "notfound" and label == "Not found"):
            out.append(i)
    return out


async def run(args, rows: dict[int, dict], store, get_pack) -> None:
    """rows: row_idx -> {name, loc, city, lat, lon, rkey, scraped}."""
    done = store.claude_done()
    todo = [i for i in rows if i not in done]
    if args.claude_limit:
        todo = todo[: args.claude_limit]
    web = args.claude_web_searches if args.claude_web else 0
    log.info("Claude review: %d rows to check (%d already checked) with %s, effort %s, web search %s",
             len(todo), len(done), args.claude_model, args.claude_effort, f"up to {web}/row" if web else "off")
    if not todo:
        return

    if args.claude_dry_run:
        i = todo[0]
        r = rows[i]
        prompt = build_prompt(r, get_pack(i))
        per_row = _estimate_row_usd(args.claude_model, prompt, web, args.claude_batch)
        log.info("DRY RUN - nothing sent. Example prompt for row %d:\n%s\n%s\n%s", i, "-" * 60, prompt, "-" * 60)
        log.info("Rough estimate: ~$%.3f per row -> ~$%.0f for %d rows (can vary +/-50%%%s)", per_row,
                 per_row * len(todo), len(todo), "; web searches add more" if web else "")
        return

    if args.claude_batch:
        return run_batch(args, rows, todo, store, get_pack, web)

    spend = Spend(budget=args.claude_budget)
    verifier = Verifier(args.claude_model, args.claude_effort, web, spend)
    sem = asyncio.Semaphore(args.claude_workers)
    stop = asyncio.Event()
    t0 = time.monotonic()

    async def one(i: int):
        if stop.is_set():
            return
        async with sem:
            if stop.is_set():
                return
            if spend.over():
                log.warning("Claude budget of $%.2f reached - stopping review (raise --claude-budget to continue)",
                            spend.budget)
                stop.set()
                return
            r = rows[i]
            cached = store.claude_by_key(r["rkey"])
            if cached:
                store.save_claude(i, r["rkey"], cached)
                return
            prompt = build_prompt(r, get_pack(i))
            try:
                v = to_row(await verifier.verify(prompt), args.claude_model)
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                log.error("Claude API key rejected: %s", e)
                stop.set()
                return
            except anthropic.BadRequestError as e:
                log.error("Claude request rejected for row %d (%s): %s", i, r["name"], e.message)
                if "credit balance" in str(e.message).lower():
                    stop.set()
                return
            except (anthropic.APIError, RuntimeError) as e:
                log.warning("Claude failed on row %d (%s): %s - will retry next run", i, r["name"], e)
                return
            store.save_claude(i, r["rkey"], v)
            spend.rows += 1
            el = time.monotonic() - t0
            log.info("[claude %d/%d] %s -> %s (%s) | %s | %.0f rows/min", spend.rows, len(todo), r["name"],
                     v["display"] or "unknown", v["confidence"], f"${spend.usd:.2f}", spend.rows / el * 60)

    try:
        await asyncio.gather(*(one(i) for i in todo))
    finally:
        log.info("Claude review: %s", spend)
        await verifier.client.close()


# ----------------------------------------------------------------------------- batch mode (50% cheaper)

BATCH_CHUNK = 10000


def _estimate_row_usd(model: str, prompt: str, web: int, batch: bool) -> float:
    pin, pout = PRICES.get(model, (5, 25))
    out_tokens = 400 if "haiku" in model else 1200
    usd = (len(prompt) / 3.5 + 900) * pin / 1e6 + out_tokens * pout / 1e6
    if batch:
        usd *= 0.5
    return usd + (1.5 * WEB_SEARCH_USD if web else 0)


def _copy_duplicates(rows: dict[int, dict], store) -> int:
    n, done = 0, store.claude_done()
    for i, r in rows.items():
        if i not in done:
            v = store.claude_by_key(r["rkey"])
            if v:
                store.save_claude(i, r["rkey"], v)
                n += 1
    return n


def run_batch(args, rows: dict[int, dict], todo: list[int], store, get_pack, web: int) -> None:
    store.db.execute("CREATE TABLE IF NOT EXISTS claude_batches(id TEXT PRIMARY KEY, model TEXT, row_ids TEXT, "
                     "status TEXT, created TEXT)")
    client = anthropic.Anthropic(max_retries=5)
    spend = Spend(budget=args.claude_budget)
    verifier = Verifier(args.claude_model, args.claude_effort, web, spend)

    pending = store.db.execute("SELECT id FROM claude_batches WHERE status='submitted'").fetchall()
    if pending:
        log.info("Found %d batch(es) submitted earlier - collecting those instead of submitting new ones", len(pending))
    else:
        _copy_duplicates(rows, store)
        done = store.claude_done()
        seen_keys, send = set(), []
        for i in todo:
            if i in done or rows[i]["rkey"] in seen_keys:
                continue
            seen_keys.add(rows[i]["rkey"])
            send.append(i)
        if not send:
            log.info("Nothing left to send to Claude")
            return
        prompts = {i: build_prompt(rows[i], get_pack(i)) for i in send}
        est = {i: _estimate_row_usd(args.claude_model, p, web, True) for i, p in prompts.items()}
        total, keep = 0.0, []
        for i in send:
            if args.claude_budget is not None and total + est[i] > args.claude_budget:
                break
            total += est[i]
            keep.append(i)
        if len(keep) < len(send):
            log.warning("Budget $%.2f covers about %d of %d unique buildings - sending those. "
                        "Re-run with a higher --claude-budget for the rest.", args.claude_budget, len(keep), len(send))
        log.info("Submitting %d buildings to Claude (%s, batch = half price). Estimated cost ~$%.2f",
                 len(keep), args.claude_model, total)
        for start in range(0, len(keep), BATCH_CHUNK):
            chunk = keep[start: start + BATCH_CHUNK]
            reqs = [{"custom_id": f"r{i}",
                     "params": verifier.base_params([{"role": "user", "content": prompts[i]}])} for i in chunk]
            b = client.messages.batches.create(requests=reqs)
            store.db.execute("INSERT INTO claude_batches VALUES(?,?,?,?,datetime('now'))",
                             (b.id, args.claude_model, json.dumps(chunk), "submitted"))
            store.db.commit()
            log.info("Submitted batch %s with %d requests", b.id, len(chunk))
        pending = store.db.execute("SELECT id FROM claude_batches WHERE status='submitted'").fetchall()

    try:
        for (bid,) in pending:
            while True:
                b = client.messages.batches.retrieve(bid)
                c = b.request_counts
                if b.processing_status == "ended":
                    break
                log.info("Batch %s: %d processing, %d done, %d errored - checking again in 60s "
                         "(safe to close; re-run the same command later to collect)",
                         bid, c.processing, c.succeeded, c.errored)
                time.sleep(60)
            saved = failed = 0
            for res in client.messages.batches.results(bid):
                i = int(res.custom_id[1:])
                if res.result.type != "succeeded":
                    failed += 1
                    continue
                msg = res.result.message
                spend.add(msg, args.claude_model, discount=0.5)
                v = next((dict(bl.input) for bl in msg.content
                          if bl.type == "tool_use" and bl.name == "record_verdict"), None)
                if v is None or i not in rows:
                    failed += 1
                    continue
                store.save_claude(i, rows[i]["rkey"], to_row(v, args.claude_model))
                spend.rows += 1
                saved += 1
            store.db.execute("UPDATE claude_batches SET status='collected' WHERE id=?", (bid,))
            store.db.commit()
            log.info("Batch %s collected: %d verdicts saved, %d without a verdict (re-run to retry those)",
                     bid, saved, failed)
    except KeyboardInterrupt:
        log.warning("Stopped waiting. The batch keeps running at Anthropic - re-run the same command to collect it.")
        raise
    finally:
        client.close()
    copied = _copy_duplicates(rows, store)
    if copied:
        log.info("Copied verdicts to %d duplicate rows of the same buildings", copied)
    log.info("Claude batch review: %s", spend)
