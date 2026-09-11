## コミット時のルール

コミットメッセージは `Conventional Commits` 形式をベースとする。

例:

```
<type>[optional scope]: <description>

[optional body]
```

- `type` は、`fix`, `feat`, `add`, `doc`, `refactor` 等、短い一つの英単語とする。
- `description` は、60文字以内(なるべく)の日本語とする。
- `optional body` は、日本語で簡潔に記述する。`description` で十分説明できる場合は省略する。 

セッションへのリンク(`https://claude.ai/code/session_...`)は貼らない。commit message のトレーラー、PR 本文、issue やコメント、いずれも対象とする。
