# dotclaude

`~/.claude` の公開可能な設定（dotfiles）と、個人用の Claude Code plugin marketplace。

- `dotfiles/` — `~/.claude` 配下に配置する設定ファイル。`install.sh` でシンボリックリンクを張る。
- `plugins/` — 個人用 plugin。`.claude-plugin/marketplace.json` を通じて marketplace として配布する。

## 構成

```
dotclaude/
├── .claude-plugin/marketplace.json
├── plugins/
│   ├── dart-lsp/            # Dart Analysis Server を LSP として接続する
│   └── writing-style/       # commit / PR / issue / docs の文体ルール skill
├── dotfiles/
│   ├── CLAUDE.md
│   ├── settings.json                # 公開用ベース
│   ├── settings.local.json          # ローカル専用オーバーレイ (gitignore)
│   ├── settings.local.json.example
│   ├── statusline-command.sh
│   ├── agents/              # orchestrator + worker 階層の agent
│   └── scripts/
│       └── agent-usage-report.ts
├── install.sh
└── README.md
```

## インストール

### dotfiles

```bash
git clone git@github.com:yash268925/dotclaude.git ~/work/dotclaude
cd ~/work/dotclaude
./install.sh
```

`install.sh` は `dotfiles/` の各ファイルを `~/.claude/` にシンボリックリンクする。同名の実体ファイルが既にある場合は `.bak` に退避する。冪等であり、何度実行しても結果は同じ。

ただし `settings.json` だけはリンクではなく、後述のマージによって実ファイルとして生成される。

## settings.json のローカルオーバーレイ

`~/.claude/settings.json` は、次の 2 ファイルをマージして生成する。

| ファイル | 役割 |
| --- | --- |
| `dotfiles/settings.json` | 公開用のベース。リポジトリで追跡する |
| `dotfiles/settings.local.json` | マシン固有の設定 |

settings.json の任意のキーに使える。書式は `dotfiles/settings.local.json.example` を参照する。

マージは jq の `*` 演算子による再帰的なディープマージで、オブジェクトは再帰的に統合され、配列とスカラーはローカル側が優先される。jq が無い環境では python3 にフォールバックし、どちらも無ければエラー終了する。`dotfiles/settings.local.json` が存在しない場合はベースがそのままコピーされる。

```bash
cp dotfiles/settings.local.json.example dotfiles/settings.local.json
# dotfiles/settings.local.json を編集する
./install.sh
```

**`dotfiles/settings.json` および `dotfiles/settings.local.json` はリンクではないため、編集しても `install.sh` を再実行するまで `~/.claude/settings.json` には反映されない。**

生成に失敗した場合（ローカル側が不正な JSON の場合など）は既存の `~/.claude/settings.json` を書き換えずにエラー終了する。既存が実ファイルで内容が異なる場合は `.bak` に退避する。

### plugins

Claude Code 上で実行する。

```
/plugin marketplace add yash268925/dotclaude
/plugin install dart-lsp@dotclaude
/plugin install writing-style@dotclaude
```

## agents

`dotfiles/agents/` に置き、`install.sh` が `~/.claude/agents` にリンクする。

| agent | 用途 |
| --- | --- |
| `orchestrator` | 要件を確定してタスクを分割し、自分で実施するか worker に委託するかを判断する |
| `worker-heavy` | 分割できない複雑な事象、原因不明の不具合調査、広範囲の設計判断 |
| `worker-standard` | 要件が明確な実装・バグ修正・リファクタリング。標準の委託先 |
| `helper` | 定型作業、テスト / lint の実行、検索や情報収集 |

## skills

`writing-style` plugin が提供する。

| skill | 用途 |
| --- | --- |
| `github-writing` | commit message / PR / issue / ドキュメントの文体ルール。該当する文章を書くときのみ読み込まれる |

## dotfiles を plugin にしていない理由

statusLine は plugin として配布できないため、`settings.json` と `statusline-command.sh` は plugin 化できない。よって dotfiles は `install.sh` によるシンボリックリンク方式を採る。agents も plugin 経由だと `plugin-name:agent-name` の形でしか参照できず、`settings.json` の `agent` 指定や日常の呼び出しが冗長になるため dotfiles 側に置いている。`plugins/` に残すのは skill と LSP 設定のみ。
