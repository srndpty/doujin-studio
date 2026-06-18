# MVPメモ

## 方針

- 4ページ短編を最初の完成単位にする。
- Manga JSONを中間表現にし、生成、編集、レンダリング、出力を分離する。
- 画像生成はコマ単位で扱い、写植はレンダリング段で行う。
- ComfyUIは外部サービスとして接続し、利用不能時はstubに戻す。

## MVPでやらないこと

- 公式作品や既存同人誌の自動収集。
- 既存同人誌のOCR解析。
- LLMまたは画像生成モデルのfine-tuning。
- 16ページ構成、見開き、表紙、奥付。

## 次ステップ: ComfyUI実連携

- ComfyUIの`File -> Export (API)`で書き出したworkflow JSONを`workflows/default.workflow_api.json`として読む。
- アプリ側はpositive prompt、negative prompt、seed、width、height、filename prefixだけを書き換える。
- `/prompt`で生成を投入し、`/history/{prompt_id}`と`/view`で先頭画像を取得する。
- ComfyUIが利用できない場合はstub画像へ戻し、編集とレンダリングを継続できる状態にする。
