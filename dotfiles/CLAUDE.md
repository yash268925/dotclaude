- ユーザーとの対話は日本語で行う。

## git, github

- gitのコミットメッセージについて、`Conventional Commits` 形式をベースとする。
  ```
  <type>[optional scope]: <description>
  
  [optional body]
  ```

  - `type` は、`fix`, `feat`, `add`, `doc`, `refactor` 等、短い一つの英単語とする。
  - `description` は、60文字以内(なるべく)の日本語とする。
  - `optional body` は、日本語で簡潔に記述する。`description` で十分説明できる場合は省略する。 

- githubへのissue, PR, それらへのコメントについて:
  - 日本語で記述する。
  - 特定の個人に向けた文体でなく、客観的な第三者の視点から違和感がない文体で記述する。

## コードコメント

コードコメントには非自明な WHY だけを書く。書くのは隠れた制約・workaround を入れた理由・
読み手が驚く挙動といった、コードから復元できない情報に限る。

書かないもの:

- WHAT (コードを読めば分かること。`// ユーザー ID を取得する` の類)
- 変更履歴 (「〜を追加した」「旧実装では〜だった」等)
- タスク ID 参照 (`(UZU-XXXX)` 等)

docs / README も同様に、issue 参照・経緯・マイグレーション履歴は書かず、
最新仕様のスナップショットだけを書く。
