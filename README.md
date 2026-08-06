# dotclaude

`~/.claude` の公開可能な設定（dotfiles）と、個人用の Claude Code plugin marketplace をまとめたリポジトリです。

- `dotfiles/` … `~/.claude` 配下に配置する設定ファイル群。`install.sh` でシンボリックリンクを張ります。
- `plugins/` … 個人用プラグイン。`.claude-plugin/marketplace.json` を通じて marketplace として配布します。

## 構成

```
dotclaude/
├── .claude-plugin/marketplace.json
├── plugins/
│   ├── dart-lsp/            # Dart Analysis Server を LSP として接続する plugin
│   └── orchestrator-agents/ # orchestrator + worker 3階層のエージェント群
├── dotfiles/
│   ├── CLAUDE.md
│   ├── settings.json
│   ├── statusline-command.sh
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

`install.sh` は `dotfiles/` の各ファイルを `~/.claude/` にシンボリックリンクします。同名の実体ファイルが既にある場合は `.bak` に退避してからリンクを張ります。何度実行しても同じ結果になります（冪等）。

### plugins

Claude Code 上で以下を実行します。

```
/plugin marketplace add yash268925/dotclaude
/plugin install orchestrator-agents@dotclaude
/plugin install dart-lsp@dotclaude
```

## なぜ dotfiles を plugin にしないのか

statusLine は plugin として配布できない仕様のため、`settings.json` と `statusline-command.sh` は plugin 化できません。そのため dotfiles については `install.sh` によるシンボリックリンク方式を採っています。plugin で配布できるもの（agents, LSP 設定）だけを `plugins/` に置いています。

## agents

`orchestrator-agents` plugin に含まれるエージェントです。

- **orchestrator** … 要件を確定しタスクを分割して、自分でやるか worker に委託するかをクオリティとコストで判断するマネージャ。
- **worker-heavy** … 分割できない複雑な事象、原因不明の不具合調査、広範囲の設計判断を担当する最上位担当。
- **worker-standard** … 要件が明確な実装・バグ修正・リファクタリングを担当する標準の委託先。
- **worker-light** … 定型作業、テスト / lint の実行、検索や情報収集など判断を要さないタスクを低コストで担当。
