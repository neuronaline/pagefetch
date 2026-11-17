"""Universal stateless multi-profile cookie pool and RFC 6265 matching."""

from __future__ import annotations

import ipaddress
import os
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from .utils.urls import registrable_host

SameSitePolicy = Literal["Strict", "Lax", "None"]


def _is_ip_literal(host: str) -> bool:
    clean = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(clean)
        return True
    except ValueError:
        return False


def _normalize_same_site(value: Any) -> SameSitePolicy | None:
    if not value:
        return None
    lowered = str(value).strip().lower()
    if lowered == "strict":
        return "Strict"
    if lowered == "lax":
        return "Lax"
    if lowered == "none":
        return "None"
    return None


def _parse_cookie_expiry(value: Any, *, now: float | None = None) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        val = float(value)
        return val if val > 0 else None
    text = str(value).strip()
    if not text or text == "0":
        return None
    try:
        num = float(text)
        return num if num > 0 else None
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(text)
        ts = dt.timestamp()
        return ts if ts > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(slots=True, frozen=True)
class CookieRecord:
    """Normalized immutable representation of a single HTTP/browser cookie."""

    name: str
    value: str
    domain: str | None = None
    path: str = "/"
    secure: bool = False
    http_only: bool = False
    same_site: SameSitePolicy | None = None
    expires: float | None = None
    host_only: bool = False

    def __post_init__(self) -> None:
        clean_name = str(self.name).strip()
        if not clean_name:
            raise ValueError("CookieRecord.name must be a non-empty string")
        object.__setattr__(self, "name", clean_name)
        object.__setattr__(self, "value", str(self.value).strip())

        if self.domain is not None:
            raw_dom = str(self.domain).strip().lower().rstrip(".")
            if raw_dom.startswith("."):
                raw_dom = raw_dom.lstrip(".")
                object.__setattr__(self, "host_only", False)
            object.__setattr__(self, "domain", raw_dom or None)
        else:
            object.__setattr__(self, "host_only", False)

        clean_path = str(self.path or "/").strip()
        if not clean_path.startswith("/"):
            clean_path = "/" + clean_path
        object.__setattr__(self, "path", clean_path)
        object.__setattr__(self, "secure", bool(self.secure))
        object.__setattr__(self, "http_only", bool(self.http_only))
        object.__setattr__(self, "same_site", _normalize_same_site(self.same_site))
        object.__setattr__(self, "expires", _parse_cookie_expiry(self.expires))


def _path_matches(request_path: str, cookie_path: str) -> bool:
    """RFC 6265 §5.1.4 path-match algorithm."""
    req = request_path or "/"
    cpath = cookie_path or "/"
    if req == cpath:
        return True
    if req.startswith(cpath):
        if cpath.endswith("/"):
            return True
        if len(req) > len(cpath) and req[len(cpath)] == "/":
            return True
    return False


def _domain_matches(host: str, cookie_domain: str, *, host_only: bool) -> bool:
    """RFC 6265 §5.1.3 domain-match algorithm."""
    if host == cookie_domain:
        return True
    if host_only or _is_ip_literal(host):
        return False
    return host.endswith("." + cookie_domain)


def _profile_digest(records: Sequence[CookieRecord], salt: int = 0) -> str:
    parts = [
        f"{r.domain or '*'}|{int(r.host_only)}|{int(r.secure)}|{r.path}|{r.name}={r.value}"
        for r in records
    ]
    payload = f"{salt}:" + "&".join(parts)
    return sha256(payload.encode("utf-8")).hexdigest()[:12]


@dataclass(slots=True, frozen=True)
class CookieProfile:
    """An isolated cookie session/profile within a :class:`CookiePool`."""

    id: str
    records: tuple[CookieRecord, ...]
    domains: frozenset[str]

    @classmethod
    def from_records(cls, records: Iterable[CookieRecord], *, salt: int = 0) -> CookieProfile:
        # Last-record-wins within a single profile while preserving first-seen key order.
        deduped: dict[tuple[str | None, str, str], CookieRecord] = {}
        for rec in records:
            deduped[(rec.domain, rec.path, rec.name)] = rec
        rec_tuple = tuple(deduped.values())
        sites: set[str] = set()
        for rec in rec_tuple:
            if rec.domain:
                site = registrable_host(f"https://{rec.domain}") or rec.domain
                sites.add(site)
        return cls(
            id=_profile_digest(rec_tuple, salt=salt),
            records=rec_tuple,
            domains=frozenset(sites),
        )

    def match_for_url(
        self,
        url: str,
        *,
        initial_site: str | None = None,
        now: float | None = None,
    ) -> list[CookieRecord]:
        """Return cookies applicable to *url* ordered by RFC 6265 specificity."""
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            return []
        req_path = parsed.path or "/"
        current_time = time.time() if now is None else now
        current_site = registrable_host(url) or host

        indexed_matches: list[tuple[int, int, int, CookieRecord]] = []
        for idx, rec in enumerate(self.records):
            if rec.expires is not None and rec.expires <= current_time:
                continue
            if rec.secure and scheme != "https":
                continue
            if rec.same_site == "Strict" and initial_site and current_site != initial_site:
                continue
            if rec.domain is None:
                if initial_site and current_site != initial_site:
                    continue
            else:
                if not _domain_matches(host, rec.domain, host_only=rec.host_only):
                    continue
            if not _path_matches(req_path, rec.path):
                continue
            dom_len = len(rec.domain) if rec.domain else 0
            indexed_matches.append((-len(rec.path), -dom_len, idx, rec))

        indexed_matches.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in indexed_matches]

    def to_http_cookies(
        self,
        url: str,
        *,
        initial_site: str | None = None,
        now: float | None = None,
    ) -> dict[str, str]:
        """Build a name->value cookie dict for *url* (more specific paths win)."""
        matched = self.match_for_url(url, initial_site=initial_site, now=now)
        result: dict[str, str] = {}
        for rec in matched:
            if rec.name not in result:
                result[rec.name] = rec.value
        return result

    def to_playwright_cookies(
        self,
        url: str,
        *,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Build Playwright/Camoufox ``context.add_cookies`` dicts for *url*'s site."""
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            return []
        current_time = time.time() if now is None else now
        max_expiry = current_time + 86400.0 * 400
        target_site = registrable_host(url) or host
        host_is_local_or_ip = (
            _is_ip_literal(host)
            or host in {"localhost", "localhost.localdomain"}
            or "." not in target_site
        )

        payloads: list[dict[str, Any]] = []
        for rec in self.records:
            if rec.expires is not None and rec.expires <= current_time:
                continue
            if rec.secure and scheme != "https":
                continue
            is_secure = bool(rec.secure)
            same_site = rec.same_site if not (rec.same_site == "None" and not is_secure) else None
            clamped_exp = min(rec.expires, max_expiry) if rec.expires is not None else None

            item: dict[str, Any] = {"name": rec.name, "value": rec.value}
            if rec.domain is None:
                if host_is_local_or_ip:
                    cpath = rec.path if (rec.path or "/").startswith("/") else f"/{rec.path}"
                    item["url"] = f"{parsed.scheme}://{parsed.netloc}{cpath}"
                else:
                    item["domain"] = f".{target_site}"
                    item["path"] = rec.path or "/"
            else:
                rec_site = registrable_host(f"https://{rec.domain}") or rec.domain
                if rec_site != target_site and not _domain_matches(host, rec.domain, host_only=rec.host_only):
                    continue
                rec_is_local_or_ip = (
                    _is_ip_literal(rec.domain) or rec.domain == "localhost" or "." not in rec.domain
                )
                item["domain"] = rec.domain if (rec_is_local_or_ip or rec.host_only) else f".{rec.domain}"
                item["path"] = rec.path or "/"

            if is_secure and "url" not in item:
                item["secure"] = True
            if rec.http_only:
                item["httpOnly"] = True
            if same_site is not None:
                item["sameSite"] = same_site
            if clamped_exp is not None:
                item["expires"] = clamped_exp
            payloads.append(item)
        return payloads

    def overlay(self, override: CookieProfile) -> CookieProfile:
        """Return a new profile merging *override* records on top of *self*."""
        override_keys = {(r.domain, r.path, r.name) for r in override.records}
        override_unscoped_names = {r.name for r in override.records if r.domain is None}
        kept = [
            r
            for r in self.records
            if (r.domain, r.path, r.name) not in override_keys
            and r.name not in override_unscoped_names
        ]
        kept.extend(override.records)
        return CookieProfile.from_records(kept)


class CookiePool:
    """Thread-safe pool of :class:`CookieProfile` instances with rotation & cooldown."""

    __slots__ = (
        "_profiles",
        "_lock",
        "_cursor",
        "_sticky_by_site",
        "_cooldown_until",
        "_failure_counts",
    )

    def __init__(self, profiles: Iterable[CookieProfile] = ()) -> None:
        self._profiles: list[CookieProfile] = [p for p in profiles if p.records]
        self._lock = threading.Lock()
        self._cursor = 0
        self._sticky_by_site: dict[str, str] = {}
        self._cooldown_until: dict[str, float] = {}
        self._failure_counts: dict[str, int] = {}

    @classmethod
    def from_any(
        cls,
        source: Any = None,
        *,
        cookie_file: str | Path | None = None,
    ) -> CookiePool:
        return parse_cookie_pool(source, cookie_file=cookie_file)

    @property
    def profiles(self) -> tuple[CookieProfile, ...]:
        with self._lock:
            return tuple(self._profiles)

    @property
    def is_empty(self) -> bool:
        with self._lock:
            return not self._profiles

    def __len__(self) -> int:
        with self._lock:
            return len(self._profiles)

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._profiles)

    def __iter__(self) -> Iterator[CookieProfile]:
        return iter(self.profiles)

    def add(self, source: Any) -> int:
        """Parse *source* and append resulting profiles to the pool; return count added."""
        parsed = parse_cookie_pool(source)
        new_profiles = list(parsed.profiles)
        if not new_profiles:
            return 0
        with self._lock:
            offset = len(self._profiles)
            for idx, prof in enumerate(new_profiles):
                # Re-salt IDs on dynamic addition so identical values remain distinct slots.
                salted = CookieProfile.from_records(prof.records, salt=offset + idx)
                self._profiles.append(salted)
        return len(new_profiles)

    def _is_profile_eligible(self, profile: CookieProfile, url: str, site: str, now: float) -> bool:
        if not profile.domains:
            # Universal unscoped profile: ensure at least one non-expired cookie exists.
            return any(r.expires is None or r.expires > now for r in profile.records)
        if site not in profile.domains:
            return False
        if profile.match_for_url(url, initial_site=site, now=now):
            return True
        # Allow profiles scoped to a subdomain of the same site (e.g. www.example.com when
        # initial URL is https://example.com before redirect).
        return any(
            (r.expires is None or r.expires > now)
            and r.domain is not None
            and (registrable_host(f"https://{r.domain}") or r.domain) == site
            for r in profile.records
        )

    def acquire(
        self,
        url: str,
        *,
        rotation: Literal["sticky", "rotate"] = "sticky",
        exclude_ids: set[str] | None = None,
    ) -> CookieProfile | None:
        """Select the next healthy profile applicable to *url*."""
        now_wall = time.time()
        now_mono = time.monotonic()
        site = registrable_host(url) or (urlsplit(url).hostname or "").lower()
        excluded = exclude_ids or set()

        with self._lock:
            if not self._profiles:
                return None
            eligible = [
                p for p in self._profiles if self._is_profile_eligible(p, url, site, now_wall)
            ]
            if not eligible:
                return None

            healthy_ids = {
                p.id
                for p in eligible
                if p.id not in excluded and now_mono >= self._cooldown_until.get(p.id, 0.0)
            }
            if healthy_ids:
                if rotation == "sticky" and site:
                    sticky_id = self._sticky_by_site.get(site)
                    if sticky_id and sticky_id in healthy_ids:
                        for p in eligible:
                            if p.id == sticky_id:
                                return p

                total_len = len(self._profiles)
                chosen: CookieProfile | None = None
                for offset in range(total_len):
                    idx = (self._cursor + offset) % total_len
                    cand = self._profiles[idx]
                    if cand.id in healthy_ids:
                        chosen = cand
                        self._cursor = (idx + 1) % total_len
                        break
                if chosen is not None:
                    if rotation == "sticky" and site:
                        self._sticky_by_site[site] = chosen.id
                    return chosen

            # All non-excluded profiles are cooling down: prefer unexcluded cooling-down
            # profiles ordered by earliest recovery, then fall back to any eligible profile.
            unexcluded = [p for p in eligible if p.id not in excluded]
            pool_slice = unexcluded if unexcluded else eligible
            chosen = min(pool_slice, key=lambda p: self._cooldown_until.get(p.id, 0.0))
            if rotation == "sticky" and site:
                self._sticky_by_site[site] = chosen.id
            return chosen

    def report_failure(self, profile_id: str, *, cooldown_seconds: float = 120.0) -> None:
        """Put *profile_id* on exponential cooldown after a block/challenge."""
        if cooldown_seconds <= 0:
            return
        now_mono = time.monotonic()
        with self._lock:
            failures = self._failure_counts.get(profile_id, 0) + 1
            self._failure_counts[profile_id] = failures
            duration = min(cooldown_seconds * (2 ** (failures - 1)), 1800.0)
            self._cooldown_until[profile_id] = now_mono + duration
            for site, pid in list(self._sticky_by_site.items()):
                if pid == profile_id:
                    del self._sticky_by_site[site]

    def report_success(self, profile_id: str) -> None:
        """Clear failure counters and active cooldown for *profile_id*."""
        with self._lock:
            self._failure_counts.pop(profile_id, None)
            self._cooldown_until.pop(profile_id, None)

    def stats(self) -> dict[str, int]:
        """Return pool health summary counts."""
        now_mono = time.monotonic()
        with self._lock:
            total = len(self._profiles)
            cooling = sum(
                1 for p in self._profiles if now_mono < self._cooldown_until.get(p.id, 0.0)
            )
            return {"total": total, "healthy": total - cooling, "cooling_down": cooling}

    def cache_token_for_url(self, url: str) -> str | None:
        """Return a deterministic cache-key token for profiles matching *url*, or ``None``."""
        now_wall = time.time()
        site = registrable_host(url) or (urlsplit(url).hostname or "").lower()
        with self._lock:
            if not self._profiles:
                return None
            ids = [
                p.id
                for p in self._profiles
                if self._is_profile_eligible(p, url, site, now_wall)
            ]
        if not ids:
            return None
        return sha256(":".join(ids).encode("utf-8")).hexdigest()[:16]

    def session(
        self,
        url: str,
        *,
        rotation: Literal["sticky", "rotate"] = "sticky",
        cooldown: float = 120.0,
        override: CookiePool | None = None,
    ) -> CookieSession:
        """Create a per-operation :class:`CookieSession` for *url* and optional *override* pool."""
        if override is None or override.is_empty:
            return CookieSession(
                self,
                url,
                rotation=rotation,
                cooldown=cooldown,
            )
        if len(override) == 1 and not self.is_empty:
            return CookieSession(
                self,
                url,
                rotation=rotation,
                cooldown=cooldown,
                per_call_overlay=override.profiles[0],
            )
        now_wall = time.time()
        site = registrable_host(url) or (urlsplit(url).hostname or "").lower()
        with self._lock:
            eligible_base = [
                p for p in self._profiles if self._is_profile_eligible(p, url, site, now_wall)
            ]
        base_prof = eligible_base[0] if len(eligible_base) == 1 else None
        return CookieSession(
            override,
            url,
            rotation=rotation,
            cooldown=cooldown,
            base_profile=base_prof,
        )


class CookieSession:
    """Per-operation cookie profile selector coordinating HTTP, Browser, and retries."""

    __slots__ = (
        "pool",
        "base_profile",
        "per_call_overlay",
        "cooldown",
        "url",
        "rotation",
        "current",
        "tried_ids",
        "_raw_current_id",
    )

    def __init__(
        self,
        pool: CookiePool,
        url: str,
        *,
        rotation: Literal["sticky", "rotate"],
        cooldown: float,
        base_profile: CookieProfile | None = None,
        per_call_overlay: CookieProfile | None = None,
    ) -> None:
        self.pool = pool
        self.url = url
        self.rotation = rotation
        self.cooldown = cooldown
        self.base_profile = base_profile
        self.per_call_overlay = per_call_overlay
        self.tried_ids: set[str] = set()
        raw = pool.acquire(url, rotation=rotation)
        if raw is not None:
            self.tried_ids.add(raw.id)
        self._raw_current_id = raw.id if raw is not None else None
        self.current = self._compose(raw)

    def _compose(self, profile: CookieProfile | None) -> CookieProfile | None:
        composed = self.base_profile
        if profile is not None:
            composed = composed.overlay(profile) if composed is not None else profile
        if self.per_call_overlay is not None:
            composed = (
                composed.overlay(self.per_call_overlay)
                if composed is not None
                else self.per_call_overlay
            )
        return composed

    def rotate(self, _attempt: int = 1) -> CookieProfile | None:
        if self._raw_current_id is not None:
            self.pool.report_failure(self._raw_current_id, cooldown_seconds=self.cooldown)
        nxt = self.pool.acquire(self.url, rotation="rotate", exclude_ids=self.tried_ids)
        if nxt is not None:
            self.tried_ids.add(nxt.id)
            self._raw_current_id = nxt.id
        self.current = self._compose(nxt)
        return self.current

    def finalize(self, *, success: bool, blocked: bool = False) -> None:
        if self._raw_current_id is None:
            return
        if success:
            self.pool.report_success(self._raw_current_id)
        elif blocked:
            self.pool.report_failure(self._raw_current_id, cooldown_seconds=self.cooldown)



def _parse_header_line(line: str) -> list[CookieRecord]:
    text = line.strip()
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    records: list[CookieRecord] = []
    for segment in text.split(";"):
        part = segment.strip()
        if not part or "=" not in part:
            continue
        name, val = part.split("=", 1)
        clean_name = name.strip()
        if not clean_name:
            continue
        # Skip Set-Cookie attribute directives if accidentally pasted
        if clean_name.lower() in {"path", "domain", "expires", "max-age", "samesite"}:
            continue
        records.append(CookieRecord(name=clean_name, value=val.strip()))
    return records


def _looks_like_netscape_text(text: str) -> bool:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith(("# netscape http cookie file", "# http cookie file", "#httponly_")):
            return True
        if line.startswith("#"):
            continue
        if line.count("\t") >= 6:
            return True
    return False


def _parse_netscape_text(text: str, *, salt_offset: int = 0) -> list[CookieProfile]:
    records: list[CookieRecord] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        http_only = False
        working = raw_line.rstrip("\r\n")
        if working.lstrip().lower().startswith("#httponly_"):
            http_only = True
            idx = working.lower().find("#httponly_")
            working = working[idx + len("#httponly_") :]
        elif stripped.startswith("#"):
            continue

        parts = working.split("\t", 6)
        if len(parts) < 7:
            continue
        raw_domain, include_subdomains, path, secure_str, expiry_str, name, value = parts
        clean_name = name.strip()
        if not clean_name:
            continue
        dom = raw_domain.strip().lower()
        sub_flag = include_subdomains.strip().upper() == "TRUE"
        host_only = not dom.startswith(".") and not sub_flag
        clean_dom = dom.lstrip(".").rstrip(".") or None
        secure = secure_str.strip().upper() == "TRUE"
        expires = _parse_cookie_expiry(expiry_str)
        records.append(
            CookieRecord(
                name=clean_name,
                value=value.strip(),
                domain=clean_dom,
                path=path.strip() or "/",
                secure=secure,
                http_only=http_only,
                expires=expires,
                host_only=host_only,
            )
        )
    return [CookieProfile.from_records(records, salt=salt_offset)] if records else []


def _profiles_from_path(path: Path, *, salt_offset: int = 0) -> list[CookieProfile]:
    expanded = path.expanduser()
    if not expanded.exists():
        raise ValueError(f"cookie path not found: {str(path)!r}")
    if expanded.is_dir():
        raise ValueError(f"cookie_file must be a file, not a directory: {str(path)!r}")

    if expanded.suffix.lower() == ".json":
        raise ValueError("JSON cookie files are not supported; use Netscape cookies.txt or dict/Cookie header inputs")

    content = expanded.read_text(encoding="utf-8-sig")
    stripped = content.lstrip()
    if stripped.startswith(("{", "[")):
        raise ValueError("JSON cookie content is not supported; use Netscape cookies.txt or raw Cookie lines")
    if _looks_like_netscape_text(content):
        return _parse_netscape_text(content, salt_offset=salt_offset)

    # Line-delimited raw Cookie header profiles (1 non-comment line = 1 profile)
    line_profiles: list[CookieProfile] = []
    for idx, line in enumerate(content.splitlines()):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        recs = _parse_header_line(s)
        if recs:
            line_profiles.append(CookieProfile.from_records(recs, salt=salt_offset + idx))
    return line_profiles


def _safe_path_exists(value: str) -> bool:
    if not value or len(value) > 1024 or "\x00" in value or "\n" in value or "\r" in value:
        return False
    try:
        return os.path.exists(os.path.expanduser(value))
    except (OSError, ValueError):
        return False


def _looks_like_explicit_filepath(value: str) -> bool:
    if "\n" in value or "\r" in value or ";" in value or len(value) > 1024:
        return False
    if value.startswith(("./", "../", "/", "~/", ".\\", "..\\")):
        return True
    if "=" not in value and value.lower().endswith((".txt", ".cookies", ".jar", ".json")):
        return True
    return False


def _parse_single_source(source: Any, *, salt_offset: int = 0) -> list[CookieProfile]:
    if source is None:
        return []
    if isinstance(source, CookiePool):
        return list(source.profiles)
    if isinstance(source, CookieProfile):
        return [source] if source.records else []
    if isinstance(source, CookieRecord):
        return [CookieProfile.from_records((source,), salt=salt_offset)]
    if isinstance(source, Path):
        return _profiles_from_path(source, salt_offset=salt_offset)
    if isinstance(source, Mapping):
        if type(source).__module__.startswith(("http.cookies", "httpx")):
            raise ValueError(
                f"Unsupported cookies input of type {type(source).__name__}; expected dict, CookiePool, "
                "CookieProfile, Cookie header str, Path, or Sequence of profiles"
            )
        recs = [
            CookieRecord(name=str(k).strip(), value=str(v).strip())
            for k, v in source.items()
            if str(k).strip()
        ]
        return [CookieProfile.from_records(recs, salt=salt_offset)] if recs else []
    if isinstance(source, str):
        stripped = source.strip()
        if not stripped:
            return []
        if stripped.startswith(("{", "[")):
            raise ValueError(
                "JSON cookie strings are not supported; use dict, CookiePool, CookieProfile, "
                "raw Cookie headers, or Netscape cookies.txt"
            )
        if _looks_like_explicit_filepath(stripped) or (
            "=" not in stripped and ";" not in stripped and _safe_path_exists(stripped)
        ):
            return _profiles_from_path(Path(stripped), salt_offset=salt_offset)
        if _looks_like_netscape_text(stripped):
            return _parse_netscape_text(stripped, salt_offset=salt_offset)
        if "\n" in stripped:
            profiles: list[CookieProfile] = []
            for idx, line in enumerate(stripped.splitlines()):
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                recs = _parse_header_line(s)
                if recs:
                    profiles.append(CookieProfile.from_records(recs, salt=salt_offset + idx))
            return profiles
        recs = _parse_header_line(stripped)
        return [CookieProfile.from_records(recs, salt=salt_offset)] if recs else []
    if isinstance(source, Sequence) and not isinstance(source, (bytes, bytearray)):
        items = list(source)
        if not items:
            return []
        if all(isinstance(x, CookieRecord) for x in items):
            return [CookieProfile.from_records(items, salt=salt_offset)]

        # Backwards compatibility: list of simple "k=v" pairs (e.g. CLI --cookie a=1 --cookie b=2)
        if all(
            isinstance(x, str)
            and ";" not in x
            and "\n" not in x
            and not _looks_like_explicit_filepath(x.strip())
            and "=" in x
            for x in items
        ):
            parsed_pairs = [_parse_header_line(x) for x in items]
            flat = [r for sub in parsed_pairs for r in sub]
            names = [r.name for r in flat]
            if len(names) == len(set(names)):
                return [CookieProfile.from_records(flat, salt=salt_offset)] if flat else []

        profiles = []
        for idx, item in enumerate(items):
            profiles.extend(_parse_single_source(item, salt_offset=salt_offset + idx * 1000))
        return profiles

    raise ValueError(
        f"Unsupported cookies input of type {type(source).__name__}; expected dict, CookiePool, "
        "CookieProfile, Cookie header str, Path, or Sequence of profiles"
    )


def load_cookies(path: str | Path) -> CookiePool:
    """Load a :class:`CookiePool` from a Netscape ``cookies.txt`` or header list file."""
    return CookiePool(_profiles_from_path(Path(path)))


def parse_cookie_pool(
    source: Any = None,
    *,
    cookie_file: str | Path | None = None,
) -> CookiePool:
    """Normalize any supported cookie source (and optional *cookie_file*) into a :class:`CookiePool`."""
    profiles: list[CookieProfile] = []
    if cookie_file is not None:
        profiles.extend(_profiles_from_path(Path(cookie_file), salt_offset=0))
    if source is not None:
        profiles.extend(_parse_single_source(source, salt_offset=len(profiles) * 1000 + 1))
    return CookiePool(profiles)
