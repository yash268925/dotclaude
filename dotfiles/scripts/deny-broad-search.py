#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash): ルートやホーム全体を再帰探索するコマンドを拒否する。

find / grep -r / rg / fd / ag / du / ls -R / tree などの探索起点が `/`、`~`、`$HOME`、
`/Users` など広すぎる場合に permissionDecision: deny を返す。
起点の省略や相対パスは hook 入力の cwd と、同一コマンド内の `cd` を追って解決する。
シェルの完全な解析ではなく、複合コマンド・引用符・コマンド置換・`bash -c`・
リダイレクト・ヒアドキュメントといったよくある形だけを扱う。
想定外の入力や解決できないパスはすべて許可側に倒す。
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


class Cmd(list):
    """1 コマンド分の単語リスト。cd の追跡と暗黙の起点の判定に必要な文脈を持つ。"""

    def __init__(self, words=(), kind="cmd"):
        super().__init__(words)
        self.kind = kind  # "cmd" | "open" / "close" (サブシェルの括弧)
        self.nested = []  # このコマンド内のコマンド置換などの文字列
        self.stdin = False  # パイプやリダイレクトで標準入力を受ける
        self.isolated = False  # パイプ・バックグラウンドのため cd が外に影響しない


def read_heredoc_delim(text, i):
    """`<<` の直後 i から区切り語を読み、(区切り語, `<<-` か, 次の位置) を返す。"""
    n = len(text)
    j = i
    strip_tabs = j < n and text[j] == "-"
    if strip_tabs:
        j += 1
    while j < n and text[j] in " \t":
        j += 1
    start = j
    buf = []
    while j < n and text[j] not in " \t\n;&|()<>":
        c = text[j]
        if c == "\\" and j + 1 < n:
            buf.append(text[j + 1])
            j += 2
        elif c in "'\"":
            k = text.find(c, j + 1)
            if k < 0:
                k = n
            buf.append(text[j + 1:k])
            j = k + 1
        else:
            buf.append(c)
            j += 1
    if j == start:
        return None
    return "".join(buf), strip_tabs, j


def skip_heredoc_bodies(text, i, queue):
    """改行直後の i から、キュー内の各ヒアドキュメントの本文を読み飛ばした位置を返す。"""
    n = len(text)
    for delim, strip_tabs in queue:
        while i < n:
            j = text.find("\n", i)
            line = text[i:] if j < 0 else text[i:j]
            i = n if j < 0 else j + 1
            if (line.lstrip("\t") if strip_tabs else line) == delim:
                break
    queue.clear()
    return i


def parse(text):
    """コマンド文字列を Cmd のリストに分解する。入れ子のコマンド文字列は各 Cmd.nested に入る。"""
    segments = []
    nested = []
    words = []
    buf = []
    in_word = False
    skip_next = False
    stdin = False
    isolated = False
    heredocs = []
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
        nonlocal words, nested, skip_next, stdin, isolated
        end_word()
        if words or nested:
            seg = Cmd(words)
            seg.nested = nested
            seg.stdin = stdin
            seg.isolated = isolated
            segments.append(seg)
            words = []
            nested = []
            stdin = False
            isolated = False
        skip_next = False

    def isolate_previous():
        if segments and segments[-1].kind == "cmd":
            segments[-1].isolated = True

    def match_paren(start):
        depth = 1
        j = start
        queue = []
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
            if ch == "<" and text.startswith("<<", j):
                if text.startswith("<<<", j):
                    j += 3
                    continue
                hd = read_heredoc_delim(text, j + 2)
                if hd:
                    queue.append(hd[:2])
                    j = hd[2]
                    continue
            if ch == "\n" and queue:
                j = skip_heredoc_bodies(text, j + 1, queue)
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
        heredoc = None
        if c == "<" and nxt == "<" and text[i + 2:i + 3] != "<":
            heredoc = read_heredoc_delim(text, i + 2)

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
        elif heredoc:
            if in_word and not "".join(buf).isdigit():
                end_word()
            buf = []
            in_word = False
            stdin = True
            heredocs.append(heredoc[:2])
            i = heredoc[2]
        elif c in "<>" or (c == "&" and nxt == ">"):
            # `2>/dev/null` の 2 のようにファイル記述子だけの単語は演算子に取り込む。
            if in_word and not "".join(buf).isdigit():
                end_word()
            buf = []
            in_word = False
            if c == "<":
                stdin = True
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
        elif c == "\n":
            end_segment()
            i = skip_heredoc_bodies(text, i + 1, heredocs) if heredocs else i + 1
        elif c == "|":
            end_segment()
            if nxt != "|":
                isolate_previous()
                stdin = True
                isolated = True
            i += 2 if nxt in ("|", "&") else 1
        elif c == "&":
            end_segment()
            if nxt != "&":
                isolate_previous()
            i += 2 if nxt == "&" else 1
        elif c in "()":
            end_segment()
            segments.append(Cmd(kind="open" if c == "(" else "close"))
            i += 1
        elif c == ";":
            end_segment()
            i += 1
        else:
            buf.append(c)
            in_word = True
            i += 1

    end_segment()
    return segments


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
    paths = []
    while i < len(args):
        a = args[i]
        if a in ("-H", "-L", "-P", "-E", "-x", "-s", "-X", "-d") or re.fullmatch(r"-O\d", a):
            i += 1
        elif a == "-D":
            i += 2
        elif a == "-f" and i + 1 < len(args):
            paths.append(args[i + 1])
            i += 2
        else:
            break
    while i < len(args):
        a = args[i]
        if a.startswith("-") or a in ("(", ")", "!", ","):
            break
        paths.append(a)
        i += 1
    return paths


def grep_paths(args, extra_short="", extra_long=()):
    """再帰検索でなければ None を返す。"""
    opts, operands = scan(
        args,
        short_val=set("efmABCdD" + extra_short),
        long_val={
            "--regexp", "--file", "--max-count", "--after-context",
            "--before-context", "--context", "--directories", "--devices",
            "--include", "--exclude", "--exclude-dir", "--exclude-from",
            "--label", "--binary-files", *extra_long,
        },
    )
    found = names(opts)
    recursive = (
        {"-r", "-R", "--recursive", "--dereference-recursive"} & found
        or "recurse" in values(opts, "-d", "--directories")
    )
    if not recursive:
        return None
    has_pattern = {"-e", "-f", "--regexp", "--file"} & found
    return operands if has_pattern else operands[1:]


def ugrep_paths(args):
    return grep_paths(args, "J", {"--glob", "--iglob", "--file-type", "--jobs", "--replace"})


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
    paths = operands[1:] + values(opts, "--search-path")
    base = values(opts, "--base-directory", "-C")
    if base:
        # 相対の検索パスは base 基準になる。
        paths = [p if re.match(r"[/~$]", p) else posixpath.join(base[-1], p) for p in paths]
        paths += base
    return paths


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
    return None


def tree_paths(args):
    _, operands = scan(
        args,
        short_val=set("LPIoHT"),
        long_val={"--charset", "--filelimit", "--timefmt", "--sort"},
    )
    return operands


def eza_paths(args):
    opts, operands = scan(
        args,
        short_val=set("LIts"),
        long_val={
            "--level", "--ignore-glob", "--sort", "--time", "--time-style",
            "--color", "--colour", "--color-scale", "--colour-scale",
        },
    )
    if {"-R", "-T", "--recurse", "--tree"} & names(opts):
        return operands
    return None


XARGS_SHORT_VAL = set("adEIJLnPRsS")
XARGS_LONG_VAL = {
    "--arg-file", "--delimiter", "--eof", "--max-args", "--max-procs",
    "--max-lines", "--max-chars", "--process-slot-var",
}


def xargs_command(args):
    """xargs 自身のオプションを読み飛ばした、実行対象のコマンドを返す。"""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a.startswith("--"):
            i += 2 if "=" not in a and a in XARGS_LONG_VAL else 1
        elif a.startswith("-") and len(a) > 1:
            for k in range(1, len(a)):
                if a[k] in XARGS_SHORT_VAL:
                    if k == len(a) - 1:
                        i += 1
                    break
            i += 1
        else:
            break
    return args[i:]


# 起点を省略したとき、パイプやリダイレクトの標準入力を検索するコマンド。
READS_STDIN = {"grep", "egrep", "fgrep", "ggrep", "ugrep", "rg", "ag"}

CHECKERS = {
    "find": find_paths,
    "gfind": find_paths,
    "grep": grep_paths,
    "egrep": grep_paths,
    "fgrep": grep_paths,
    "ggrep": grep_paths,
    "ugrep": ugrep_paths,
    "eza": eza_paths,
    "exa": eza_paths,
    "rg": rg_paths,
    "fd": fd_paths,
    "fdfind": fd_paths,
    "ag": ag_paths,
    "du": du_paths,
    "ls": ls_paths,
    "tree": tree_paths,
}


def resolve(word, cwd=None):
    """探索起点を絶対パスに正規化する。解決できないもの (不明な変数や cwd 不明の相対パス) は None を返す。"""
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
        elif cwd:
            m = re.match(r"^\$(?:PWD\b|\{PWD\})(/.*)?$", p)
            if m:
                p = cwd + (m.group(1) or "")

    if not p.startswith("/"):
        if not cwd or not p or "$" in p or "`" in p:
            return None
        p = posixpath.join(cwd, p)
    path = posixpath.normpath(re.sub(r"^/+", "/", p))
    # /System/Volumes/Data は / の別名 (firmlink)。実体パスに直してから判定する。
    while True:
        m = re.match(r"/system/volumes/data(?=/|$)", path, re.IGNORECASE)
        if not m:
            return path
        path = path[m.end():] or "/"


def broad_path(word, cwd=None):
    """探索起点が広すぎる場合は正規化したパスを、そうでなければ None を返す。"""
    # `~user` 単体はそのユーザーのホームであり、pw_dir が /var/root のように標準の場所にないことがある。
    if re.fullmatch(r"~[^/]+/*", word):
        return resolve(word.rstrip("/"))
    path = resolve(word, cwd)
    if path is None:
        return None
    lower = path.lower()
    if (
        lower in BROAD_DIRS
        or lower == HOME.lower()
        or re.fullmatch(r"/(users|home)/[^/]+", lower) is not None
    ):
        return path
    return None


def next_cwd(args, cwd):
    """`cd` / `pushd` の実行後のカレントディレクトリ。分からなければ None。"""
    operands = []
    for k, a in enumerate(args):
        if a == "--":
            operands = args[k + 1:]
            break
        if not (a.startswith("-") and a != "-"):
            operands.append(a)
    if not operands:
        return HOME
    target = operands[0]
    if target == "-" or target.startswith("+"):
        return None
    return resolve(target, cwd)


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


def is_info_only(name, args):
    """`--version` や `--help` のように、探索しない呼び出し。"""
    if "--version" in args or "--help" in args:
        return True
    return name in ("rg", "fd", "fdfind") and ("-V" in args or "-h" in args)


def check_command(words, cwd, stdin, from_xargs, depth, violations):
    words = strip_prefix(words)
    if not words:
        return
    name = posixpath.basename(words[0])
    args = words[1:]

    if name in SHELLS:
        for k, a in enumerate(args):
            if re.fullmatch(r"-[a-z]*c[a-z]*", a) and k + 1 < len(args):
                violations.extend(analyze(args[k + 1], cwd, depth + 1))
                break
    elif name == "eval":
        violations.extend(analyze(" ".join(args), cwd, depth + 1))
    elif name == "xargs":
        check_command(xargs_command(args), cwd, stdin, True, depth, violations)
    elif name in CHECKERS:
        paths = CHECKERS[name](args)
        if paths is None:
            return
        if not paths:
            # xargs は標準入力から起点を追加で受け取る。
            if from_xargs or (stdin and name in READS_STDIN) or is_info_only(name, args):
                return
            paths = ["."]
        for path in paths:
            resolved = broad_path(path, cwd)
            if resolved:
                violations.append((name, path, resolved))


def analyze(command, cwd=None, depth=0):
    """(コマンド名, 探索起点, 正規化した起点) の違反リストを返す。"""
    if depth > 5:
        return []
    violations = []
    saved = []

    for seg in parse(command):
        if seg.kind == "open":
            saved.append(cwd)
            continue
        if seg.kind == "close":
            if saved:
                cwd = saved.pop()
            continue

        for inner in seg.nested:
            violations.extend(analyze(inner, cwd, depth + 1))
        words = strip_prefix(seg)
        if not words:
            continue
        name = posixpath.basename(words[0])
        if name in ("cd", "pushd", "popd") and not seg.isolated:
            cwd = None if name == "popd" else next_cwd(words[1:], cwd)
        else:
            check_command(words, cwd, seg.stdin, False, depth, violations)
    return violations


def build_reason(name, path, resolved):
    shown = f"`{path}`" if path == resolved else f"`{path}` (= `{resolved}`)"
    return (
        f"`{name}` の探索起点 {shown} が広すぎます。"
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
        cwd = data.get("cwd")
        cwd = posixpath.normpath(cwd) if isinstance(cwd, str) and cwd.startswith("/") else None
        violations = analyze(command, cwd)
    except Exception:
        return 0

    if violations:
        name, path, resolved = violations[0]
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": build_reason(name, path, resolved),
                }
            },
            sys.stdout,
            ensure_ascii=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
