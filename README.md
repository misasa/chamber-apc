# python package -- chamber-apc

Chamber APC is a Python package for automatic pressure control in a chamber. It includes a web panel for controlling pressure and a simulated device for testing without hardware.

## Dependency

- Python 3.9以上（Windows / macOS）

インストール時に依存ライブラリ（Flask、Werkzeug、HTTPX、Trio）も入ります。通信はPython内のHTTPXで行います。curlなどの外部通信コマンドは不要です。

## Installation

GitHubから取得します。

```sh
git clone https://github.com/misasa/chamber-apc.git
cd chamber-apc
```

このフォルダで、初回に実行します。

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Windowsでは `python -m venv .venv`、有効化は `cmd.exe` で `.venv\Scripts\activate.bat` を使います。

## Commands

| Command | Description |
| --- | --- |
| `chamber-apc-control` | Open the web panel for manual and scheduled pressure control. |
| `chamber-apc-mock` | Run a simulated APC device. |

## Usage

模擬装置を１つ目のターミナルで起動すると、模擬装置の画面がブラウザで開きます。
自動で開く必要がない場合は `chamber-apc-mock --no-browser` または `APC_NO_BROWSER=1` を使います。

```sh
chamber-apc-mock
```

２つ目のターミナルでも仮想環境を有効にして、制御画面を起動します。

```sh
chamber-apc-control
```

- 制御画面: http://127.0.0.1:5000/
- 模擬装置: http://127.0.0.1:8001/

仮想環境を有効にしていれば、インストール後は別のフォルダからも起動できます。

制御画面は自動でブラウザを開きます（`--no-browser` または `APC_NO_BROWSER=1` で無効）。起動時にスケジュール実行スレッドも開始します。`flask run` や複数プロセスでの起動には対応していません。

起動済みの状態で `chamber-apc-control` やデスクトップのショートカットをもう一度実行すると、既存のアプリを使ってブラウザを開き直します。さらに、画面からの状態取得（2秒ごと）が1分間途切れた場合も、自動でブラウザを開き直します。ブラウザの休止でも開き直す場合があります。自動で開く試行は1分間隔です。`--no-browser` または `APC_NO_BROWSER=1` を指定した起動では、この自動表示も無効です。

装置へのコマンド送信時は、コンソールに `Command: POST <送信先URL>` を表示します。URLには圧力設定値やOn／Offの指令が含まれます。送信直前の表示なので、装置への到達・成功を保証するものではありません。結果は画面のLogsとログファイルで確認できます。定期的な状態取得はコンソールに表示しません。

macOSではAirPlayレシーバーが5000番を使っていることがあります。このとき `http://localhost:5000/` はAirPlay側へつながるため、`http://127.0.0.1:5000/` を開くか、`APC_PORT=5050 chamber-apc-control` のように別のポートで起動してください。

制御画面は `127.0.0.1` / `localhost` 宛てのリクエストだけを受け付け、別サイトからのPOST（Originが一致しないもの）は403で拒否します。ログイン機能がないため、他のWebページから装置へ指令が送られるのを防ぐ措置です。

## 配布用のwheelを作る

ソースフォルダで次のコマンドを実行すると、`dist/` に `.whl` ファイルができます。配布先では `python -m pip install` にそのファイルのパスを渡します。

```sh
python -m pip wheel --no-deps . -w dist
```

### Windowsでインストールして起動する

バージョン `1.0.1` の wheel を置いたフォルダでコマンドプロンプト（`cmd.exe`）を開き、次の4行を実行します。

```bat
python -m venv .venv
.venv\Scripts\activate.bat
python -m pip install chamber_apc-1.0.1-py3-none-any.whl
chamber-apc-control
```

次回からは、同じフォルダで `.venv\Scripts\activate.bat` と `chamber-apc-control` の2行で起動できます。

## 指令と確認

`APC_STATUS_PV` のビット割り当ては次のとおりです。

| ビット | 意味 | 値 |
|---|---|---|
| 0 | RUN（画面では REGULATION） | 1 |
| 1 | ALARM | 2 |
| 2 | ZFIN | 4 |
| 3 | REMOTE | 8 |

画面のStatusにある **SCHEDULE** は装置のビットではなく、このアプリのフラグです。スケジュールの実行中だけ緑になります。

画面のRegulationの見出しは `Regulation with P = 50 hPa` で、今の `APC_P_SV` を示します（値を読めないときは `—`）。ボタンは1つのトグルです。色が今の状態を、文字が押すとどうなるかを示します。Onのときは緑のボタンに緑の文字で **OFF**、Offのときは灰色のボタンに **ON** です。状態を読めないときは `—` で、押せません。スケジュール実行中は **OFF** を押せず、サーバー側でもOff要求を拒否します。先にScheduleの **Stop** で停止してください。既にその状態なら指令は送りません。RegulationのOn／Offは、装置のRun／Stopの指令に対応します。

REMOTEが立っているときだけ送信します。画面上のボタンとサーバー側の両方で確認します。実行中にREMOTEが落ちると、スケジュールは止まり、未送信の行は送りません。送信直前にREMOTEが落ちる場合もあるため、送信後も状態を確認します。

- On: `APC_RUN_SV=1` を一度送信し、RUNフラグが1になることを確認します。
- Off: `APC_STOP_SV=1` を一度送信し、RUNフラグが0になることを確認します。
- これらの指令値は装置側が自動で0に戻します。指令値そのものは成功判定に使いません。
- 確認期限は送信開始から10秒です。HTTP応答が成功でも、期待する状態を確認できなければエラーです。
- 同時に複数のOn／Off操作は受け付けません。確認中の別操作はエラーとして返し、指令は送りません。
- `POST /regulation/on`、`POST /regulation/off` で操作します。

実際の圧力は取得できません。`APC_P_SV` は圧力の**設定値**です。

## 用語

| 用語 | 意味 |
|---|---|
| 設定値（setpoint、`APC_P_SV`） | 要求する圧力。実際の圧力ではありません |
| 指令（command） | 設定値を一度送信し、読み戻しで確認する1回の操作 |
| スケジュール（schedule） | 日時と圧力設定値（hPa）の行の並び。時刻になった行を指令として送ります |
| ソース（source） | スケジュールの作り方。`file`（CSVファイル）か `set`（画面入力でファイルなし） |

設定値はすべてスケジュールを通して送ります。OKとPlanはラジオボタンの選択に従います。もう一度Planを押すと、セット済みの計画を置き換えます。ファイルを選ぶとFileに切り替わります。ソースの違いは作り方だけで、検証、状態、開始条件、実行、確認、エラー報告、Stop／Restart、取り出しは共通です。

画面の選択肢は3つです。

| 選択肢 | やりたいこと | 入力 | ソース |
|---|---|---|---|
| **Now** | すぐにセットする | To | `set`（1行） |
| **Ramp** | ゆっくりセットする | To、Ramp time | `set`（10秒ごとの行） |
| **File** | CSVの予定で動かす | Choose File | `file` |

**Now** のボタンは **OK** で、押すとすぐに実行します。**Ramp** と **File** のボタンは **Plan** で、押すと計画をセットし、Scheduleパネルの **Go** で開始します。

| | `set`（Now、Ramp） | `file` |
|---|---|---|
| 作り方 | Toを入れて **OK** または **Plan**（Toが空なら押せません） | **Choose File** でCSVを選び、**Plan**（選ぶまで押せません） |
| ボタンの動作 | Now（OK）: 作成してすぐ開始。Ramp（Plan）: 計画を作ってセット（送信しない） | Plan: CSVをアップロードして検証し、セット（送信しない） |
| Goの動作 | Ramp: その時点のFromから行を作り直して開始 | 開始。止めたあとはRestartで残りを再開 |
| 開始の条件 | NowのOKはREMOTEがOn（Regulationは問わない）。RampのPlanはRegulationがOn、GoはREMOTEとRegulationがOn | PlanはRegulationがOn、GoはREMOTEとRegulationがOn |
| 保存 | メモリのみ（ファイルを作らない） | アップロードをファイルに保存 |

## スケジュールの共通動作

- **開始条件：** 開始の時点で装置の状態を読み、条件を満たさなければ開始せず、保持中の予定も変更しません。制御圧力はRegulationの前に設定するため、NowだけはRegulation Offでも送れます。RampとFileはRegulation Onが必要です。
- **同時実行：** スケジュールは1つだけです。実行中に別のスケジュールは作れません。先にStopを押してください。実行中でなければ、新しいスケジュールが保持中のものを置き換えます。
- **確認：** 各行を送信し、`Get_Latest_Data` の読み戻しが指定値と **0.01 hPa以内** であることを送信開始から10秒以内に確認します。例えば12.345を送信し12.35が返る丸めは許容します。これは設定値の反映確認であり、実圧力の到達確認ではありません。
- **失敗：** 画面とログへ報告し、失敗した行は再送せず次の行へ進みます。最後の行が失敗しても状態は `completed` で、失敗数とエラーを表示します。
- **Stop／Restart：** Stopはスケジュールを停止し、進行中の送信・確認を中断します。スケジュールは保持します。`POST /schedule/start` による再開は過去の行を飛ばして続けます。実行済みの行は再送しません。
- **遅れ：** 確認に時間がかかると後続の行は遅れます。スリープ復帰や長い通信停止で60秒を超えて遅れた行は送らず、まとめて再生しません。ただし時刻を過ぎた行のうち最新の行は送ります。飛ばした行数は画面とログに表示します。このため、復帰後に設定値が大きく変わることがあります。
- **状態の監視：** 実行中にRegulationがOffになるかREMOTEが落ちると、RampとFileのスケジュールは止まり、未送信の行は送りません。Readyの計画はそのまま残ります。ALARMがONの場合はランプを赤で表示し、警告を出します。ALARMがONの間はRegulationのOn、Go、NowのOKを断り、実行中のRampとFileは止めます（スケジュール停止後はOffを押せます）。ブラウザーを閉じていても、実行中は2秒ごとに装置の状態を確認して止めます。
- **取り消し：** 中断・タイムアウトしても、装置が既に受理した指令を取り消すことはできません。
- **再起動：** PCのスリープ中やアプリ終了中には実行できません。再起動後は作り直します。

| 入出力 | 内容 |
|---|---|
| `POST /schedule/set` | ソース `set`。`ramp_seconds` があれば計画を保持（開始しない）。なければ `to` をすぐ開始 |
| `POST /schedule/upload` | ソース `file`。`csv_file` を検証して保持（開始しない） |
| `POST /schedule/start` | 保持中のスケジュールを開始・再開（`revision`）。`set` はここで `from` を装置から読み、行を作る |
| `POST /schedule/end` | 停止 |
| `GET /api/schedule` | 状態、名前、ソース、次の行、行番号、失敗数、飛ばした数 |
| `GET /api/schedule/rows` | 保持中の全行をグラフ用のJSONで返す |
| `GET /schedule/csv` | ソースによらず、保持中の行をCSV形式のテキストで返す。画面の **[detail]** はこれをブラウザーの別タブで開く |

## Set（`set`）

Nowは **To** だけを表示し、OKですぐに送ります。Rampは **From** から **To** へ変化させ、Planで計画をScheduleパネルにセットしてGoで開始します。Fromは装置から取得した現在の `APC_P_SV` で、編集できません。Toにはページの読み込み時とNowまたはRampを選んだときに今の `APC_P_SV` が入ります。2秒ごとの更新では入れず、入力中の値は上書きしません。Rampは、Toに値があればそのまま残します。

- 起点の値は、Goを押した時点で装置から改めて読みます。行の時刻もGoから数えます。
- 上下矢印は **0.1 hPa刻み** です。
- ToとCSVの値は **0〜100 hPa** の範囲だけ受け付けます。実験に合わせて `APC_P_MIN` と `APC_P_MAX` で変更できます（不正な値なら起動しません）。範囲は起動時の表示とログで確認できます。今の設定値が範囲外ならRampは作れないので、先にNowで範囲内へ戻します。
- RampとFileでは、各行を送る前に今の設定値との差を確かめ、**50 hPa** を超えるとスケジュールを止めます（`APC_P_MAX_STEP` で変更可）。Nowには適用しません。
- **Now** は、OKですぐ送る1行のスケジュールです。
- **Ramp** は **10秒ごと** の行になります。10分なら60行です。途中の値は小数2桁に丸め、同じ値の行は作りません。最後の行は入力したToです。
- 現在値を読めない場合は作りません。FromとToの差が0.01 hPa以内なら何も送りません。
- ToとRamp timeを入れると、ステップ数と変化率を画面に表示します（例: `60 steps, -2 hPa/min.`）。
- Goは毎回その時点のFromから作り直すため、Stopのあとは止めた値から滑らかにやり直せます。Restartの表示はCSVのスケジュールだけです。

## Regulationとスケジュール

右上の **?** を押すと、操作の順序（英語）を表示します。

画面の並びは、Status、Regulation、Pressure（計画を作る）、Schedule（セットされた計画を実行する）、Logsの順です。PressureでNow、Ramp、Fileのどれかを選ぶと、選んだほうの入力だけを表示します。Nowの **OK** はすぐ送ります。RampとFileの **Plan** は計画をScheduleパネルにセットするだけで、**Go** で開始します。**Stop** で停止します。REMOTEの案内はREMOTEフラグがOffのときだけ表示します。

Regulationなしでは圧力は変わらないため、ランプとScheduleのスケジュールはRegulation Offでは動かしません。Nowは設定値を送るだけなので、この対象外です。

- スケジュール実行中はRegulationのOFFを受け付けません。ScheduleのStopで停止してから、OFFを押してください。拒否した場合は指令を送らず、スケジュールも変更しません。
- 装置側でOffになった場合も、状態の取得時または次の行を送る前に検出して止め、`Regulation is off. Schedule stopped.` を表示します。その行は送りません。
- 再開はRegulation Onのあとです。CSVのスケジュールはRestartで残りの行を送り、SetはGoでその時点のFromから作り直します。

## ファイル（`file`）

Choose FileでCSVを選んでPlanを押すと、アップロードと形式チェックを行い、成功するとScheduleパネルの名前と次の行が更新されます。RegulationがOff、または装置と通信できない状態ではPlanを押せません（`Turn Regulation on.` と表示します）。読み込んだだけでは送信せず、Goを押すと開始します。

```csv
date,time,pressure
2026-11-08,15:00:00,50.00
2026-11-08,15:00:30,49.75
2026-11-08,15:01:00,49.50
...
2026-11-08,15:10:00,45.00
```

ソースフォルダの `schedule.csv` は、2026-11-08 15:00 から10分かけて50 hPaを10%（45 hPa）まで、30秒ごとに下げる例です。使うときは日時を書き換え、Choose Fileで選んでください。

画面の **CSV format** の例は、装置から読んだ今の `APC_P_SV` を起点に、次の正時から10分かけて30秒ごとに10%下げる21行で、状態を読むたびに作り直します。そのままファイルに保存して使えます。

`date` は `YYYY-MM-DD`、`time` は `HH:MM:SS`（実行PCのローカル時刻）、`pressure` は hPa で入力します。旧形式の `datetime` と `P (hPa)` の2列も読み込めます。UTF-8（BOMありも可）で保存してください。日時の重複、タイムゾーン付き日時、列の欠落・重複、余分な値、非数値・無限大・NaNは読み込み時に拒否します。行は日時順に並べて実行します。アップロードの上限はリクエスト全体で5 MiBです。

- 読み込みからGoまでに時刻を過ぎた行は、開始時に飛ばします。
- 内容を検証してメモリに保持するため、読み込み済みのCSVを外部で編集しても反映されません。変更後は選び直してください。
- 読み込みに失敗した場合、保持中のスケジュールは変わりません。

## 模擬装置での確認

初期状態は全フラグOFF、設定値50です。模擬装置の画面でREMOTEをONにしてから、制御画面のRegulationのOn／Offを試してください。ALARM/ZFIN/REMOTEを保持したままRUNだけを切り替えます。指令用SVは処理後0に戻ります。

模擬装置の画面は1秒ごとに状態、障害モード、受信回数、直近の指令を更新します。編集中のチェックボックスと設定値は上書きしません。別のポートで起動するには `chamber-apc-mock --port 8002`（または `MOCK_PORT`）を使います。

「Communication test」で異常時の応答を切り替えられます。

| モード | 対象 | 動作 |
|---|---|---|
| normal | － | 通常応答 |
| http_error | 状態・指令 | 指定したHTTPステータス（初期値503）を返す。指令は反映しない |
| accept_then_error | 指令 | 指令を反映してからHTTPエラーを返す |
| drop_connection | 状態・指令 | 応答せず接続を切る。指令は反映しない |
| redirect | 状態・指令 | HTTP 302を返す。指令は反映しない |
| malformed | 状態 | JSONではない本文を返す |
| empty | 状態 | 空の本文を返す |
| bad_status | 状態 | 範囲外の `APC_STATUS_PV`（99）を返す |
| no_setpoint | 状態 | `APC_P_SV` を省略する |
| slow | 状態・指令 | 指定秒数応答を遅延する（初期値12秒）。指令は遅延後に反映する |
| trickle | 状態・指令 | 指定秒数かけて本文を少しずつ返す |
| ignore_control | 指令 | Run／Stopを受理するがRUNを変えない |
| ignore_setpoint | 指令 | Setを受理するが設定値を変えない |
| remote_drop | 指令 | Run／Stopを受理してREMOTEを落とし、RUNは変えない |
| alarm_on_run | 指令 | Runを受理してALARMを立て、RUNはOFFのままにする |

設定項目は次のとおりです。障害が起きないときの応答も変えられます。

| 項目 | 内容 | 初期値 |
|---|---|---|
| `delay` | slow／trickleの秒数（0〜60） | 12 |
| `http_status` | http_error／accept_then_errorのステータス（400〜599） | 503 |
| `fail_count` | 対象リクエストをこの回数処理したらnormalへ戻す | なし（戻らない） |
| `run_delay` | Run／StopのあとRUNが変わるまでの秒数（0〜60） | 0 |
| `setpoint_delay` | 設定値の指令のあと設定値が変わるまでの秒数（0〜60） | 0 |
| `setpoint_decimals` | 読み戻す設定値を丸める小数桁数（0〜6） | なし |

`ignore_control` でRunを試す場合はRUNをOFF、Stopを試す場合はRUNをONにします。`ignore_setpoint` では現在と違う設定値を送ります。いずれも10秒後に確認エラーになります。`setpoint_decimals=1` で12.34を送ると、許容差を超えて確認エラーになります。

`slow` では、クライアントの中断・タイムアウト後に装置側の処理が進む場合も再現できます。

- `GET /mock/state`: 状態、設定、反映待ちの変更、受信回数
- `GET /mock/history`: 受理した直近1000件の指令、そのときのモード、反映後の状態
- `POST /mock/reset`: 状態、設定、履歴、反映待ちの変更、受信回数の初期化
- `POST /mock/config`: JSONまたはフォームで上の設定を変更。省略した項目は現在の値を保ちます（`mode` を変えたときの `fail_count` だけは未指定なら解除）。不明な項目や範囲外の値は400で拒否し、何も変更しません
- `GET /WeatherMonitor/Set_Mock_Status?APC_STATUS_PV=8`: REMOTEだけON

模擬装置のHTTPエラーなどは試験用の挙動であり、実機のエラー応答仕様を再現したものではありません。

## 自動テスト

開発時は仮想環境を有効にし、ソースフォルダで編集可能モードでインストールしてからテストを実行します。インストール後はソースの変更がそのまま起動コマンドとテストに反映されます。

```sh
python -m pip install -e .
python -m unittest discover -s tests
```

追加のテストライブラリは不要です。テストは実機の接続先設定を使わず、127.0.0.1の一時ポートと一時保存先を使います。

| ファイル | 内容 |
|---|---|
| `test_device_faults.py` | 模擬装置の各障害に対する制御画面の動作（実HTTP） |
| `test_integration.py` | 手動操作とスケジュールの一連の動作（実HTTP） |
| `test_set_schedule.py` | 画面入力からの行の生成、開始、Stop、Restart、排他、取り出し |
| `test_interface.py` | メッセージ、外部リクエストの拒否、JSONエラー、遅延行の飛ばし、ワーカーの継続 |
| `test_processes.py` | インストール済みの `chamber-apc-mock` と `chamber-apc-control` を起動し、スケジュール実行まで確認 |
| `test_frontend.py` | 実際のページのスクリプトをNode.jsで実行。Node.jsがなければスキップ |
| その他 | CSV、スケジューラ、通信、Regulation、設定値、再開、模擬装置の単体テスト |

通常は確認期限を1秒に短縮してHTTPテストを実行します。10秒の期限そのものは仮想時計のテストで確認します。実時間で10秒待つ場合は `APC_TEST_REALTIME=1` を指定します（macOS/Linux）。

```sh
APC_TEST_REALTIME=1 python -m unittest discover -s tests
```

## 通信処理

HTTPXとTrioの非同期HTTP処理を、通常のPython関数から呼び出します。外部プロセスは起動しません。接続失敗、HTTPエラー、通信期限超過を区別して処理します。

- GET／POSTとも自動リダイレクトを行わず、2xxのみを成功とします。指令を自動再送しません。
- 通信全体の期限を管理し、本文が少しずつ届く場合にも期限を延ばしません。Regulationと設定値の送信から確認までの10秒の期限も維持します。
- 名前解決（DNS）の待機中も期限超過・中断を受け付けます。OS側の名前解決処理自体は後で終了することがありますが、その結果から指令を送信することはありません。
- Stop時には進行中のHTTP待機を中断して接続を閉じます。装置側が受理済みの指令を取り消す意味ではありません。
- 接続先へ直接通信し、環境変数によるHTTPプロキシ設定は使いません。旧 `CURL_EXE` 設定は不要です。

## 実機への接続と保存先

接続先の初期値は `http://127.0.0.1:8001/WeatherMonitor/` です。macOSの例:

```sh
export DEVICE_BASE_URL="http://192.168.1.50:8001/WeatherMonitor/"
chamber-apc-control
```

Windowsの `cmd.exe` では `set DEVICE_BASE_URL=http://192.168.1.50:8001/WeatherMonitor/` とします。

- `APC_DATA_DIR`: ログとアップロードCSVの保存先。既定は `~/deleteme.d/YYYY-MM/`（起動時の年月）
- ログ: 保存先の `chamber-apc-control.log`。5 MBごとにローテーションし、過去3ファイルを保持
- CSV: 保存先の `chamber-apc-control-schedules/`。同名でも別ファイルとして保存
- `APC_PORT`: 制御画面のポート（既定5000）
- `LOG_DISPLAY_LIMIT`: ログ表示件数の初期値（10〜1000、既定100）。画面の選択を保存している場合はそちらを優先します
- `FLASK_SECRET_KEY`: セッション署名鍵。未指定なら起動ごとに生成します（一時メッセージにのみ使用）
- 詳細ログは `chamber-apc-control --debug` または `APP_DEBUG=1` で有効にできます。

通常は指令・確認結果とエラー、状態取得の断絶と復帰を記録し、繰り返しの状態取得ログは詳細モードだけに記録します。

## ライセンス

Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka

このプログラムはフリーソフトウェアです。Free Software Foundationが公表したGNU General Public Licenseのバージョン3、または（任意で）それ以降のバージョンの条件のもとで、再配布や改変ができます（`GPL-3.0-or-later`）。

このプログラムは有用であることを願って配布しますが、商品性や特定目的への適合性の黙示の保証を含め、いかなる保証もありません。詳しくは [LICENSE](LICENSE) を参照してください。
