#!/usr/bin/env python3
import re
from collections import defaultdict

PG_RESERVED = {
    "all","analyse","analyze","and","any","array","as","asc","asymmetric",
    "authorization","binary","both","case","cast","check","collate","column",
    "constraint","create","current_catalog","current_date","current_role",
    "current_time","current_timestamp","current_user","default","deferrable",
    "desc","distinct","do","else","end","except","false","fetch","for",
    "foreign","from","grant","group","having","in","initially","intersect",
    "into","lateral","leading","limit","localtime","localtimestamp","not",
    "null","offset","on","only","or","order","placing","primary","references",
    "returning","select","session_user","some","symmetric","table","then",
    "to","trailing","true","union","unique","user","using","variadic",
    "when","where","window","with"
}

IDENT_START_RE = re.compile(r"[A-Za-z_]")
IDENT_BODY_RE = re.compile(r"[A-Za-z0-9_$]")


def normalize_identifier(identifier: str) -> str:
    s = identifier.lower()
    s = re.sub(r"[^a-z0-9_$]+", "_", s)
    s = re.sub(r"_+", "_", s)
    s = s.strip("_")

    if not s or not re.match(r"^[a-z_]", s):
        s = "_" + s

    if s in PG_RESERVED:
        s += "_"

    return s


class IdentifierTracker:
    def __init__(self):
        self.mapping = {}
        self.reverse = defaultdict(set)

    def register(self, original, normalized):
        self.mapping[original] = normalized
        self.reverse[normalized].add(original)

    def collisions(self):
        return {k: v for k, v in self.reverse.items() if len(v) > 1}


def transform_sql_fragment(sql: str, tracker: IdentifierTracker) -> str:
    out = []
    i = 0
    n = len(sql)

    while i < n:
        ch = sql[i]

        # line comment
        if ch == "-" and i + 1 < n and sql[i+1] == "-":
            out.append(sql[i:])
            break

        # block comment
        if ch == "/" and i + 1 < n and sql[i+1] == "*":
            j = i + 2
            depth = 1
            while j < n and depth > 0:
                if sql[j:j+2] == "/*":
                    depth += 1
                    j += 2
                elif sql[j:j+2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            out.append(sql[i:j])
            i = j
            continue

        # string literal
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j+1] == "'":
                        j += 2
                    else:
                        j += 1
                        break
                else:
                    j += 1
            out.append(sql[i:j])
            i = j
            continue

        # dollar string
        if ch == "$":
            m = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[i:])
            if m:
                tag = m.group(0)
                j = i + len(tag)
                end = sql.find(tag, j)
                if end == -1:
                    out.append(sql[i:])
                    break
                out.append(sql[i:end+len(tag)])
                i = end + len(tag)
                continue

        # quoted identifier
        if ch == '"':
            j = i + 1
            buf = []
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j+1] == '"':
                        buf.append('"')
                        j += 2
                    else:
                        j += 1
                        break
                else:
                    buf.append(sql[j])
                    j += 1

            original = "".join(buf)
            normalized = normalize_identifier(original)
            tracker.register(original, normalized)
            out.append(normalized)
            i = j
            continue

        # normal identifier
        if IDENT_START_RE.match(ch):
            j = i + 1
            while j < n and IDENT_BODY_RE.match(sql[j]):
                j += 1
            out.append(sql[i:j].lower())
            i = j
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def is_copy_line(line: str):
    return re.match(r"^\s*copy\b.*\bfrom\s+stdin\s*;\s*$", line, re.I)


def transform_dump_sql(sql: str):
    tracker = IdentifierTracker()
    out = []
    i = 0
    n = len(sql)
    in_copy = False

    while i < n:
        if in_copy:
            end = sql.find("\n", i)
            if end == -1:
                line = sql[i:]
                i = n
            else:
                line = sql[i:end+1]
                i = end + 1

            out.append(line)
            if line.strip() == r"\.":
                in_copy = False
            continue

        end = sql.find("\n", i)
        if end == -1:
            line = sql[i:]
            i = n
        else:
            line = sql[i:end+1]
            i = end + 1

        if is_copy_line(line):
            out.append(transform_sql_fragment(line, tracker))
            in_copy = True
        else:
            out.append(transform_sql_fragment(line, tracker))

    return "".join(out), tracker.mapping, tracker.collisions()


def transform_sql_for_qpair(sql: str):
    tracker = IdentifierTracker()
    out = transform_sql_fragment(sql, tracker)
    return out, tracker.mapping, tracker.collisions()