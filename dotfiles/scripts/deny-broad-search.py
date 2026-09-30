#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash): ルートやホーム全体を再帰探索するコマンドを拒否する。

find / grep -r / rg / fd / ag / du / ls -R / tree の探索起点が `/`、`~`、`$HOME`、
`/Users` など広すぎる場合に permissionDecision: deny を返す。
シェルの完全な解析ではなく、複合コマンド・引用符・コマンド置換・`bash -c`・
リダイレクトといったよくある形だけを扱う。想定外の入力ではすべて許可側に倒す。
"""
import json
import os
import posixpath
import pwd
import re
import sys

HOME = os.path.expanduser("~")

# 小文字で比較する。macOS の標準ボリュームは大文字小文字を区別しないため。
BROAD_DIRS = {
    "/", "/users", "/home", "/root", "/system", "/system/volumes",
    "/system/volumes/data", "/library", "/applications", "/volumes",
    "/private", "/private/var", "/private/etc", "/var", "/opt", "/usr",
    "/etc", "/bin", "/sbin", "/lib", "/dev", "/proc", "/sys", "/mnt",
    "/media", "/srv", "/cores", "/nix",
}

ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
RESERVED = {"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
# 値をとるオプションのうち、後続の単語を消費するもの。
WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"},
    "env": {"-u", "-C", "-S"},
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p"},
    "timeout": {"-k", "-s"},
    "exec": {"-a"},
    "command": set(),
    "builtin": set(),
    "nohup": set(),
    "time": set(),
    "stdbuf": set(),
}


def parse(text):
    """コマンド文字列を、セグメントごとの単語リストと、入れ子のコマンド文字列に分解する。"""
    segments = []
    nested = []
    words = []
    buf = []
    in_word = False
    skip_next = False
    i, n = 0, len(text)

    def end_word():
        nonlocal buf, in_word, skip_next
        if in_word:
            if skip_next:
                skip_next = False
            else:
                words.append("".join(buf))
        buf = []
        in_word = False

    def end_segment():
        nonlocal words, skip_next
        end_word()
        if words:
            segments.append(words)
        words = []
        skip_next = False

    def match_paren(start):
        depth = 1
        j = start
        while j < n:
            ch = text[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "'":
                k = text.find("'", j + 1)
                j = (k if k >= 0 else n) + 1
                continue
            if ch == '"':
                j += 1
                while j < n and text[j] != '"':
                    j += 2 if text[j] == "\\" else 1
                j += 1
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return j
            j += 1
        return n

    def match_backtick(start):
        j = start
        while j < n and text[j] != "`":
            j += 2 if text[j] == "\\" else 1
        return min(j, n)

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if c == "\\":
            if nxt and nxt != "\n":
                buf.append(nxt)
                in_word = True
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                j = n
            buf.append(text[i + 1:j])
            in_word = True
            i = j + 1
        elif c == '"':
            in_word = True
            i += 1
            while i < n and text[i] != '"':
                d = text[i]
                if d == "\\" and i + 1 < n:
                    if text[i + 1] in '$"\\`\n':
                        if text[i + 1] != "\n":
                            buf.append(text[i + 1])
                    else:
                        buf.append(d + text[i + 1])
                    i += 2
                elif d == "$" and text[i + 1:i + 2] == "(":
                    j = match_paren(i + 2)
                    nested.append(text[i + 2:j])
                    i = j + 1
                elif d == "`":
                    j = match_backtick(i + 1)
                    nested.append(text[i + 1:j])
                    i = j + 1
                else:
                    buf.append(d)
                    i += 1
            i += 1
        elif c == "$" and nxt == "(":
            j = match_paren(i + 2)
            nested.append(text[i + 2:j])
            in_word = True
            i = j + 1
        elif c == "`":
            j = match_backtick(i + 1)
            nested.append(text[i + 1:j])
            in_word = True
            i = j + 1
        elif c in "<>" and nxt == "(":
            j = match_paren(i + 2)
            nested.append(text[i + 2:j])
            in_word = True
            i = j + 1
        elif c in "<>" or (c == "&" and nxt == ">"):
            # `2>/dev/null` の 2 のようにファイル記述子だけの単語は演算子に取り込む。
            if in_word and not "".join(buf).isdigit():
                end_word()
            buf = []
            in_word = False
            j = i + 1 if c != "&" else i + 2
            while j < n and text[j] in "<>" and j - i < 3:
                j += 1
            if j < n and text[j] == "&" and c != "&":
                j += 1
                while j < n and (text[j].isdigit() or text[j] == "-"):
                    j += 1
            else:
                skip_next = True
            i = j
        elif c == "#" and not in_word:
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in " \t":
            end_word()
            i += 1
        elif c in ";&|()\n":
            end_segment()
            i += 1
        else:
            buf.append(c)
            in_word = True
            i += 1

    end_segment()
    return segments, nested


def scan(args, short_val=(), long_val=(), stop=()):
    """(オプション名, 値) のリストとオペランドのリストを返す。GNU 流の並べ替えを許す。"""
    opts = []
    operands = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            operands.extend(args[i:])
            break
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            if not eq and name in long_val and i < len(args):
                value = args[i]
                i += 1
            opts.append((name, value if (eq or name in long_val) else None))
            if name in stop:
                break
        elif a.startswith("-") and len(a) > 1:
            for k in range(1, len(a)):
                letter = "-" + a[k]
                if a[k] in short_val:
                    value = a[k + 1:]
                    if not value and i < len(args):
                        value = args[i]
                        i += 1
                    opts.append((letter, value))
                    break
                opts.append((letter, None))
            if opts and opts[-1][0] in stop:
                break
        else:
            operands.append(a)
    return opts, operands


def names(opts):
    return {name for name, _ in opts}


def values(opts, *wanted):
    return [v for name, v in opts if name in wanted and v]


def find_paths(args):
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-H", "-L", "-P") or re.fullmatch(r"-O\d", a):
            i += 1
        elif a == "-D":
            i += 2
        else:
            break
    paths = []
    while i < len(args):
        a = args[i]
        if a.startswith("-") or a in ("(", ")", "!", ","):
            break
        paths.append(a)
        i += 1
    return paths


def grep_paths(args):
    opts, operands = scan(
        args,
        short_val=set("efmABCdD"),
        long_val={
            "--regexp", "--file", "--max-count", "--after-context",
            "--before-context", "--context", "--directories", "--devices",
            "--include", "--exclude", "--exclude-dir", "--exclude-from",
            "--label", "--binary-files",
        },
    )
    found = names(opts)
    recursive = (
        {"-r", "-R", "--recursive", "--dereference-recursive"} & found
        or "recurse" in values(opts, "-d", "--directories")
    )
    if not recursive:
        return []
    has_pattern = {"-e", "-f", "--regexp", "--file"} & found
    return operands if has_pattern else operands[1:]


def rg_paths(args):
    opts, operands = scan(
        args,
        short_val=set("ABCeEfgmMjrtTd"),
        long_val={
            "--glob", "--iglob", "--type", "--type-not", "--type-add",
            "--type-clear", "--max-depth", "--maxdepth", "--max-count",
            "--max-filesize", "--after-context", "--before-context",
            "--context", "--regexp", "--file", "--replace", "--threads",
            "--encoding", "--sort", "--sortr", "--pre", "--pre-glob",
            "--color", "--colors", "--engine", "--ignore-file",
            "--path-separator", "--dfa-size-limit", "--regex-size-limit",
        },
    )
    has_pattern = {"-e", "-f", "--regexp", "--file", "--files", "--type-list"} & names(opts)
    return operands if has_pattern else operands[1:]


def fd_paths(args):
    opts, operands = scan(
        args,
        short_val=set("eEtdxXcSojC"),
        long_val={
            "--extension", "--exclude", "--type", "--max-depth", "--min-depth",
            "--color", "--size", "--owner", "--changed-within",
            "--changed-before", "--threads", "--search-path",
            "--base-directory", "--max-results", "--format", "--ignore-file",
            "--and", "--exec", "--exec-batch",
        },
        stop={"-x", "-X", "--exec", "--exec-batch"},
    )
    return operands[1:] + values(opts, "--search-path", "--base-directory")


def ag_paths(args):
    opts, operands = scan(
        args,
        short_val=set("ABCGgmpW"),
        long_val={
            "--after", "--before", "--depth", "--ignore", "--ignore-dir",
            "--file-search-regex", "--path-to-ignore", "--pager", "--workers",
            "--max-count", "--color-line-number", "--color-match",
            "--color-path",
        },
    )
    return operands if "-g" in names(opts) else operands[1:]


def du_paths(args):
    _, operands = scan(
        args,
        short_val=set("dBtIX"),
        long_val={
            "--max-depth", "--block-size", "--threshold", "--exclude",
            "--exclude-from", "--files0-from", "--time-style",
        },
    )
    return operands


def ls_paths(args):
    opts, operands = scan(
        args,
        long_val={
            "--ignore", "--width", "--tabsize", "--time-style", "--block-size",
            "--hide", "--sort", "--format", "--quoting-style",
        },
    )
    if {"-R", "--recursive"} & names(opts):
        return operands
    return []


def tree_paths(args):
    _, operands = scan(
        args,
        short_val=set("LPIoHT"),
        long_val={"--charset", "--filelimit", "--timefmt", "--sort"},
    )
    return operands


CHECKERS = {
    "find": find_paths,
    "grep": grep_paths,
    "egrep": grep_paths,
    "fgrep": grep_paths,
    "rg": rg_paths,
    "fd": fd_paths,
    "fdfind": fd_paths,
    "ag": ag_paths,
    "du": du_paths,
    "ls": ls_paths,
    "tree": tree_paths,
}


def resolve(word):
    """探索起点を絶対パスに正規化する。相対パスや不明な変数は None を返す。"""
    p = word
    while p.endswith(("/*", "/**")):
        p = p[:p.rindex("/")] if p.endswith("/*") else p[:-3]
        if p == "":
            p = "/"
            break

    if p == "~" or p.startswith("~/"):
        p = HOME + p[1:]
    elif p.startswith("~"):
        user, _, rest = p[1:].partition("/")
        try:
            p = pwd.getpwnam(user).pw_dir + ("/" + rest if rest else "")
        except KeyError:
            return None
    else:
        m = re.match(r"^\$(?:HOME\b|\{HOME\})(/.*)?$", p)
        if m:
            p = HOME + (m.group(1) or "")

    if not p.startswith("/"):
        return None
    return posixpath.normpath(re.sub(r"^/+", "/", p))


def is_broad(word):
    # `~user` 単体はそのユーザーのホームであり、pw_dir が /var/root のように標準の場所にないことがある。
    if re.fullmatch(r"~[^/]+/*", word):
        return resolve(word.rstrip("/")) is not None
    path = resolve(word)
    if path is None:
        return False
    lower = path.lower()
    return (
        lower in BROAD_DIRS
        or lower == HOME.lower()
        or re.fullmatch(r"/(users|home)/[^/]+", lower) is not None
    )


def strip_prefix(words):
    """代入・予約語・sudo や env などのラッパーを取り除き、実コマンドの位置を返す。"""
    i = 0
    while i < len(words):
        w = words[i]
        base = posixpath.basename(w)
        if w in RESERVED or ASSIGNMENT.match(w):
            i += 1
        elif base in WRAPPERS:
            valued = WRAPPERS[base]
            i += 1
            while i < len(words):
                if words[i] in valued:
                    i += 2
                elif words[i].startswith("-") or (base == "env" and ASSIGNMENT.match(words[i])):
                    i += 1
                else:
                    break
            if base == "timeout" and i < len(words):
                i += 1
        else:
            break
    return words[i:]


def analyze(command, depth=0):
    """(コマンド名, 探索起点) の違反リストを返す。"""
    if depth > 5:
        return []
    violations = []
    segments, nested = parse(command)
    pending = list(nested)

    for words in segments:
        words = strip_prefix(words)
        if not words:
            continue
        name = posixpath.basename(words[0])
        args = words[1:]

        if name in SHELLS:
            for k, a in enumerate(args):
                if re.fullmatch(r"-[a-z]*c[a-z]*", a) and k + 1 < len(args):
                    pending.append(args[k + 1])
                    break
        elif name == "eval":
            pending.append(" ".join(args))
        elif name in CHECKERS:
            for path in CHECKERS[name](args):
                if is_broad(path):
                    violations.append((name, path))

    for inner in pending:
        violations.extend(analyze(inner, depth + 1))
    return violations


def build_reason(name, path):
    return (
        f"`{name}` の探索起点 `{path}` が広すぎます。"
        "ファイルシステムのルートやホーム全体の再帰探索は、時間がかかる上に"
        "無関係な結果や権限エラーが大量に出るため禁止しています。"
        "次のいずれかで対応してください。"
        "(1) 探索対象をプロジェクト内、または具体的なディレクトリ"
        "(例: ~/work/foo, /usr/local/lib/xxx)に絞って再実行する。"
        "(2) ファイル名やコードの検索には Glob / Grep ツールを使う。"
        "(3) 対象の場所が分からない場合は、広く探索せずユーザーに場所を尋ねる。"
    )


def main():
    try:
        data = json.load(sys.stdin)
        command = data["tool_input"]["command"]
        violations = analyze(command)
    except Exception:
        return 0

    if violations:
        name, path = violations[0]
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": build_reason(name, path),
                }
            },
            sys.stdout,
            ensure_ascii=False,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
