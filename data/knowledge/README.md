# ローカル作品知識

作品ごとに`<work_id>/work.json`を置き、`documents`から同じディレクトリのJSONを参照します。

```json
{
  "work_id": "sample-work",
  "work_name": "表示する作品名",
  "description": "知識パックの説明",
  "documents": [
    { "file": "required.json", "usage": "required" },
    { "file": "reference.json", "usage": "reference" }
  ]
}
```

知識ファイルは`kind`、`title`、`content`、`policy`、`tags`を持つオブジェクトの配列です。変更後はストーリー生成画面で新規セッションを作成するとDBへ再同期されます。
