import cv2
import numpy as np
import time
import sys
import threading
import json
import os
from flask import Flask, render_template, Response, request, jsonify

# Telloライブラリのインポート
try:
    from djitellopy import Tello
    TELLO_AVAILABLE = True
except ImportError:
    TELLO_AVAILABLE = False

# --- 設定値 ---
WIDTH = 360
HEIGHT = 240
THRES_VAL = 70
MIN_CONTOUR_AREA = 200
MAX_CONTOUR_AREA = 5000

# 制御パラメータ
KP = 0.4
KD = 0.5
FORWARD_SPEED = 30   # ストレート最高速度
TARGET_HEIGHT = 45


# --- スレッド共有用の状態クラス ---
class DroneState:
    def __init__(self):
        # 制御パラメータ（Web UIから動的変更可能）
        self.kp = KP
        self.kd = KD
        self.forward_speed = FORWARD_SPEED
        self.thres_val = THRES_VAL
        self.min_area = MIN_CONTOUR_AREA
        self.max_area = MAX_CONTOUR_AREA
        self.roi_start_pct = 0
        self.target_height = TARGET_HEIGHT

        # 動的ステータス
        self.battery = 0
        self.current_height = 0
        self.last_error = 0
        self.in_flight = False

        # 学習機能ステータス
        self.lap_count = 0
        self.is_recording = False
        self.lap_start_time = 0
        self.memory = []          # 現在のラップの記録データ
        self.course_map = []      # 確定したコースマップ（全ラップの平均）
        self.map_index = 0        # 現在のマップ上の位置
        self.cumulative_dist = 0  # 累積距離（フレーム数ベース）
        
        # 保存されたコースマップがあれば読み込む
        try:
            if os.path.exists("course_map.json"):
                with open("course_map.json", "r") as f:
                    self.course_map = json.load(f)
                    print(f"保存されたコースマップを読み込みました ({len(self.course_map)}ポイント)")
                    self.lap_count = 1  # 既に1周している扱いにする
        except Exception as e:
            print(f"コースマップの読み込みに失敗しました: {e}")
        self.lost_counter = 0
        self.error = 0
        self.yaw_speed = 0
        self.error_history = []  # 直近のエラー履歴（カーブ予測用）

        # マウスアシスト用ステータス
        self.mouse_active = False
        self.mouse_dx = 0
        self.mouse_dy = 0

        # 表示用ステータス
        self.system_logs = []
        self.current_forward_speed = 0
        self.line_found = False
        self.is_simulation = False

        # 画像フレームのバッファ
        self.frame_camera = None
        self.frame_thresh = None

        # スレッド制御用
        self.lock = threading.Lock()
        self.running = True
        self.cmd_queue = None

    def add_log(self, message):
        """システムログに追記する（UIとターミナルの両方に出す）"""
        timestamp = time.strftime('%H:%M:%S')
        log_msg = f"[{timestamp}] {message}"
        self.system_logs.append(log_msg)
        if len(self.system_logs) > 20:
            self.system_logs.pop(0)
        print(log_msg)


state = DroneState()

# --- Flask アプリケーションの定義 ---
app = Flask(__name__, template_folder='templates', static_folder='static')


@app.route('/')
def index():
    return render_template('index.html')


def generate_video_stream(feed_type):
    """ビデオストリーム送出用ジェネレータ"""
    while state.running:
        frame = None
        with state.lock:
            if feed_type == 'camera':
                frame = state.frame_camera
            else:
                frame = state.frame_thresh

        if frame is not None:
            try:
                ret, jpeg = cv2.imencode('.jpg', frame)
                if ret:
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
            except Exception:
                pass

        time.sleep(0.033)


@app.route('/video_feed')
def video_feed():
    return Response(generate_video_stream('camera'),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/thresh_feed')
def thresh_feed():
    return Response(generate_video_stream('threshold'),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/status')
def status_feed():
    """リアルタイムステータス送信 (Server-Sent Events)"""
    def event_stream():
        import json
        while state.running:
            time.sleep(0.1)
            with state.lock:
                data = {
                    "battery": state.battery,
                    "current_height": state.current_height,
                    "in_flight": state.in_flight,
                    "lap_count": state.lap_count,
                    "is_recording": state.is_recording,
                    "lost_counter": state.lost_counter,
                    "error": state.error,
                    "yaw_speed": state.yaw_speed,
                    "logs": list(state.system_logs),
                    "forward_speed": state.current_forward_speed,
                    "line_found": state.line_found,
                    "is_simulation": state.is_simulation,
                    "params": {
                        "kp": state.kp,
                        "kd": state.kd,
                        "forward_speed": state.forward_speed,
                        "thres_val": state.thres_val,
                        "min_area": state.min_area,
                        "max_area": state.max_area,
                        "roi_start_pct": state.roi_start_pct,
                        "target_height": state.target_height
                    }
                }
            yield f"data: {json.dumps(data)}\n\n"

    return Response(event_stream(), mimetype="text/event-stream")


@app.route('/update_params', methods=['POST'])
def update_params():
    """パラメータ更新用API"""
    param = request.form.get('param')
    value = request.form.get('value')
    try:
        with state.lock:
            if param == 'kp':
                state.kp = float(value)
            elif param == 'kd':
                state.kd = float(value)
            elif param == 'forward_speed':
                state.forward_speed = int(value)
            elif param == 'thres_val':
                state.thres_val = int(value)
            elif param == 'min_area':
                state.min_area = int(value)
            elif param == 'max_area':
                state.max_area = int(value)
            elif param == 'roi_start_pct':
                state.roi_start_pct = int(value)
            elif param == 'target_height':
                state.target_height = int(value)
        return jsonify(success=True)
    except Exception as e:
        return jsonify(success=False, message=str(e))


@app.route('/control', methods=['POST'])
def control():
    """手動フライトコントロール用API"""
    action = request.form.get('action')
    if action in ['takeoff', 'land', 'emergency', 'lap']:
        with state.lock:
            state.cmd_queue = action
        return jsonify(success=True, message=f"Action {action} queued")
    return jsonify(success=False, message="Invalid action")


@app.route('/mouse_control', methods=['POST'])
def mouse_control():
    """マウスアシスト入力用API"""
    active = request.form.get('active', 'false') == 'true'
    dx = float(request.form.get('dx', 0))
    dy = float(request.form.get('dy', 0))
    with state.lock:
        state.mouse_active = active
        state.mouse_dx = dx
        state.mouse_dy = dy
    return jsonify(success=True)


# --- ドローン制御 ＆ 画像処理ループ (バックグラウンドスレッド) ---
def drone_loop():
    global state
    tello = None
    webcam = None
    frame_read = None

    # ========== 1. 接続フェーズ ==========
    if TELLO_AVAILABLE:
        while state.running:
            try:
                state.add_log("Telloに接続中...")
                tello = Tello()
                tello.connect()
                # ストリームを一度リセットしてから開始（映像パケット詰まり対策）
                try:
                    tello.streamoff()
                except Exception:
                    pass
                tello.streamon()
                state.add_log("Telloに接続し、ストリームを開始しました")
                state.battery = tello.get_battery()
                time.sleep(2)
                frame_read = tello.get_frame_read()
                state.add_log(f"接続成功! バッテリー: {state.battery}%")
                break
            except Exception as e:
                state.add_log(f"Tello接続エラー: {e} (5秒後に再試行)")
                time.sleep(5)
    else:
        print("[エラー] djitellopy ライブラリがインストールされていません。")
        import os
        os._exit(1)

    # ========== 2. シミュレーション初期化 ==========
    if state.is_simulation:
        state.battery = 100
        webcam = cv2.VideoCapture(0)
        if webcam.isOpened():
            print("Webカメラを検出しました。")
        else:
            print("Webカメラが検出されません。ダミー映像を使用します。")
            webcam = None

    last_battery_update = time.time()

    # ========== 3. メインループ ==========
    try:
        while state.running:
            try:
                # --- 3.1 コマンド要求の確認・処理 ---
                cmd = None
                with state.lock:
                    if state.cmd_queue:
                        cmd = state.cmd_queue
                        state.cmd_queue = None

                if cmd:
                    if state.is_simulation:
                        if cmd == 'takeoff':
                            state.in_flight = True
                        elif cmd == 'land':
                            state.in_flight = False
                        elif cmd == 'emergency':
                            state.in_flight = False
                    else:
                        if cmd in ['takeoff', 'land', 'emergency']:
                            # 非同期で実行（映像ループをブロックしない）
                            def _run_cmd(c, t=tello):
                                try:
                                    state.add_log(f"{c} 処理中...")
                                    if c == 'takeoff':
                                        t.takeoff()
                                        state.in_flight = True
                                    elif c == 'land':
                                        t.land()
                                        state.in_flight = False
                                    elif c == 'emergency':
                                        t.emergency()
                                        state.in_flight = False
                                    state.add_log(f"{c} 完了")
                                except Exception as e:
                                    state.add_log(f"コマンドエラー({c}): {e}")
                            threading.Thread(target=_run_cmd, args=(cmd,), daemon=True).start()
                        if cmd == 'lap':
                            if not state.is_recording:
                                state.add_log("ラップ記録開始 (Lap 1)")
                                state.is_recording = True
                                state.lap_count = 1
                                state.lap_start_time = time.time()
                                state.memory = []
                                state.cumulative_dist = 0
                            else:
                                # 記録終了 → マップを構築/更新
                                if len(state.memory) > 10:
                                    if len(state.course_map) == 0:
                                        # 初回: そのままマップとして採用
                                        state.course_map = state.memory.copy()
                                        state.add_log(f"コースマップ構築完了 ({len(state.course_map)}ポイント)")
                                    else:
                                        # 2回目以降: 既存マップと平均化して精度を上げる
                                        new_map = []
                                        map_len = min(len(state.course_map), len(state.memory))
                                        for i in range(map_len):
                                            old = state.course_map[i]
                                            new = state.memory[i]
                                            merged = {
                                                'error': (old['error'] + new['error']) / 2,
                                                'angle': (old['angle'] + new['angle']) / 2,
                                                'yaw': (old['yaw'] + new['yaw']) / 2,
                                                'speed': (old['speed'] + new['speed']) / 2,
                                            }
                                            new_map.append(merged)
                                        state.course_map = new_map
                                        state.add_log(f"コースマップ更新完了 (Lap {state.lap_count + 1}, {len(state.course_map)}pt)")
                                    
                                    # PCにファイルとして保存
                                    try:
                                        with open("course_map.json", "w") as f:
                                            json.dump(state.course_map, f, indent=2)
                                        state.add_log("コースデータを course_map.json に保存しました")
                                    except Exception as e:
                                        state.add_log(f"データ保存エラー: {e}")

                                state.is_recording = False
                                state.lap_count += 1
                                state.lap_start_time = time.time()
                                state.cumulative_dist = 0
                                state.map_index = 0

                # --- 3.2 映像フレームの取得 ---
                if state.is_simulation:
                    if webcam:
                        ret, frame = webcam.read()
                        if not ret:
                            frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
                    else:
                        frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
                        frame[:] = 220
                        t_val = time.time()
                        center_offset = int(100 * np.sin(t_val * 0.4))
                        line_x = (WIDTH // 2) + center_offset
                        cv2.line(frame, (line_x, 0), (line_x, HEIGHT), (40, 40, 40), 25)
                        cv2.rectangle(frame, (20, 20), (35, 35), (50, 50, 50), -1)

                    img = cv2.resize(frame, (WIDTH, HEIGHT))

                    if time.time() - last_battery_update > 4:
                        last_battery_update = time.time()
                        if state.in_flight:
                            state.battery = max(0, state.battery - 1)
                        else:
                            state.battery = max(0, state.battery - 0.1)
                        state.battery = round(state.battery, 1)

                    with state.lock:
                        state.current_height = state.target_height
                else:
                    frame = frame_read.frame
                    if frame is None:
                        time.sleep(0.01)
                        continue
                    img = cv2.resize(frame, (WIDTH, HEIGHT))

                    # バッテリーと高度の取得（非同期でブロックしない）
                    if time.time() - last_battery_update > 3:
                        last_battery_update = time.time()
                        def _update(t=tello):
                            try:
                                b = t.get_battery()
                                h = t.get_distance_tof()
                                with state.lock:
                                    state.battery = b
                                    state.current_height = h
                            except Exception:
                                pass
                        threading.Thread(target=_update, daemon=True).start()

                # --- 3.3 パラメータ取得 ---
                with state.lock:
                    kp = state.kp
                    kd = state.kd
                    forward_speed_base = state.forward_speed
                    thres_val = state.thres_val
                    min_contour_area = state.min_area
                    max_contour_area = state.max_area
                    roi_start_pct = state.roi_start_pct
                    target_height = state.target_height
                    current_height = state.current_height

                # --- 3.4 画像処理 ---
                current_roi_start_y = int(HEIGHT * (roi_start_pct / 100.0))
                current_roi_start_y = min(current_roi_start_y, HEIGHT - 1)

                roi = img[current_roi_start_y:HEIGHT, 0:WIDTH]
                
                # HSV変換による「黒色・細線」専用抽出
                hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
                # thres_val を明度（Value）の上限として使用（暗いものだけを抽出）
                lower_black = np.array([0, 0, 0])
                upper_black = np.array([180, 255, thres_val])
                mask = cv2.inRange(hsv, lower_black, upper_black)
                
                # ノイズ除去
                kernel = np.ones((3, 3), np.uint8)
                thresh = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

                # --- 3.5 輪郭検出と重心の算出 ---
                contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

                line_found = False
                cx, cy_global = WIDTH // 2, HEIGHT // 2
                error = 0
                line_angle = 0

                if len(contours) > 0:
                    sorted_contours = sorted(contours, key=cv2.contourArea, reverse=True)

                    for cnt in sorted_contours:
                        area = cv2.contourArea(cnt)
                        if min_contour_area < area < max_contour_area:
                            rect = cv2.minAreaRect(cnt)
                            (rw, rh) = rect[1]

                            min_len = min(rw, rh)
                            if min_len < 0.1:
                                continue

                            aspect_ratio = max(rw, rh) / min_len

                            # 描画用の座標計算
                            box = cv2.boxPoints(rect)
                            box = np.intp(box)
                            for i in range(4):
                                box[i][1] += current_roi_start_y

                            # アスペクト比 2.5未満 → ノイズとして無視（厳密な線の判定）
                            if aspect_ratio < 2.5:
                                cv2.drawContours(img, [box], 0, (0, 0, 255), 2)
                                cv2.putText(img, f"Noise({aspect_ratio:.1f})",
                                            (box[0][0], box[0][1] - 10),
                                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
                                continue

                            # 角度の計算
                            angle = rect[2]
                            if rw < rh:
                                angle = angle - 90
                            target_angle = angle + 90
                            if target_angle > 90:
                                target_angle -= 180
                            line_angle = target_angle

                            M = cv2.moments(cnt)
                            if M['m00'] > 0:
                                cx = int(M['m10'] / M['m00'])
                                cy = int(M['m01'] / M['m00'])
                                cy_global = cy + current_roi_start_y
                                line_found = True

                                # 採用したラインを緑で描画
                                cv2.drawContours(img, [box], 0, (0, 255, 0), 3)
                                cv2.putText(img, f"A:{int(area)} R:{aspect_ratio:.1f}",
                                            (box[0][0], box[0][1] - 10),
                                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                                break

                # --- 3.6 制御値の決定 ---
                up_down_speed = 0
                if state.in_flight and not state.is_simulation:
                    if 10 <= current_height <= 200:
                        height_error = target_height - current_height
                        if abs(height_error) > 10:
                            up_down_speed = int(height_error * 0.3)
                            up_down_speed = max(min(up_down_speed, 20), -20)

                if line_found:
                    state.lost_counter = 0
                    error = cx - (WIDTH // 2)

                    # PD制御 + 角度ベースのヨー制御
                    pd_yaw = int(error * kp + (error - state.last_error) * kd)
                    state.last_error = error

                    # 直角カーブ対応：ラインの角度で機首を振る
                    angle_yaw = int(line_angle * 0.8)
                    pd_yaw += angle_yaw

                    # --- リアルタイムカーブ予測 ---
                    state.error_history.append(error)
                    if len(state.error_history) > 15:
                        state.error_history.pop(0)

                    # 予測スコア（0〜1の範囲、高いほどカーブが近い）
                    curve_score = 0.0

                    # (A) エラー変化率による予測
                    if len(state.error_history) >= 5:
                        recent = state.error_history[-5:]
                        error_deltas = [abs(recent[i] - recent[i-1]) for i in range(1, len(recent))]
                        avg_delta = sum(error_deltas) / len(error_deltas)
                        same_direction = all(recent[i] * recent[i-1] > 0 for i in range(1, len(recent)))
                        if avg_delta > 3 and same_direction:
                            curve_score = max(curve_score, min(avg_delta / 15.0, 1.0))
                            # カーブ方向に事前旋回
                            pre_yaw = int(min(avg_delta * 1.0, 35))
                            if recent[-1] > 0:
                                pd_yaw += pre_yaw
                            else:
                                pd_yaw -= pre_yaw

                    # (B) 現在のエラー量による評価
                    abs_error = abs(error)
                    if abs_error > 30:
                        curve_score = max(curve_score, min(abs_error / 120.0, 1.0))

                    # (C) ライン角度による評価
                    abs_angle = abs(line_angle)
                    if abs_angle > 15:
                        curve_score = max(curve_score, min(abs_angle / 60.0, 1.0))

                    # --- レーシングスタイルの速度制御 ---
                    # curve_score に応じて速度を連続的に変化させる
                    # score=0 → 全速（forward_speed_base）、score=1 → 最低速度（8）
                    min_speed = 8
                    forward_speed = int(forward_speed_base - (forward_speed_base - min_speed) * curve_score)
                    forward_speed = max(forward_speed, min_speed)

                    # --- コースマップ 学習・活用 ---
                    ff_yaw = 0
                    state.cumulative_dist += 1  # フレームカウントを位置として使用

                    if state.is_recording:
                        # 記録モード: 現在の状態をマップに保存
                        state.memory.append({
                            'error': error,
                            'angle': line_angle,
                            'yaw': pd_yaw,
                            'speed': forward_speed,
                        })
                    elif len(state.course_map) > 0:
                        # マップ活用モード: 10フレーム先の情報を先読み
                        lookahead = 10
                        idx = state.map_index + lookahead
                        if idx < len(state.course_map):
                            future = state.course_map[idx]
                            future_err = abs(future['error'])
                            future_angle = abs(future['angle'])

                            # マップ上でカーブが来ると分かっている場合
                            if future_err > 25 or future_angle > 20:
                                # 事前減速
                                forward_speed = int(forward_speed * 0.4)
                                # 事前旋回
                                ff_yaw = int(future['yaw'] * 0.3)

                        # マップの現在位置を進める
                        state.map_index = min(state.map_index + 1, len(state.course_map) - 1)

                    yaw_speed = pd_yaw + ff_yaw
                    yaw_speed = max(min(yaw_speed, 80), -80)
                else:
                    state.lost_counter += 1
                    forward_speed = 0

                    # --- コース復帰アルゴリズム ---
                    if state.in_flight:
                        if state.mouse_active:
                            # [マウスアシスト] ラインロスト時のみマウスで導く
                            # dx (左右) -> 旋回, dy (上下) -> 前進後退
                            yaw_speed = int(state.mouse_dx * 0.4)
                            forward_speed = int(-state.mouse_dy * 0.3)
                            
                            # 安全のためのリミット
                            yaw_speed = max(min(yaw_speed, 60), -60)
                            forward_speed = max(min(forward_speed, 30), -30)
                            
                            # 映像に警告表示
                            cv2.putText(img, "MOUSE ASSIST", (WIDTH//2 - 60, HEIGHT//2),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
                        else:
                            last_err = state.last_error  # 最後に見たラインの方向

                            if state.lost_counter <= 30:
                                # Phase 1 (約1秒): 最後に見えた方向へ旋回して探す
                                if last_err > 0:
                                    yaw_speed = 40  # 右に旋回
                                elif last_err < 0:
                                    yaw_speed = -40  # 左に旋回
                                else:
                                    yaw_speed = 30  # デフォルトは右

                            elif state.lost_counter <= 90:
                                # Phase 2 (約2秒): 逆方向に大きく旋回
                                if last_err > 0:
                                    yaw_speed = -50
                                else:
                                    yaw_speed = 50

                            elif state.lost_counter <= 180:
                                # Phase 3 (約3秒): 後退しながら旋回して元の場所に戻る
                                forward_speed = -10  # ゆっくり後退
                                yaw_speed = 40 if last_err > 0 else -40

                            else:
                                # Phase 4 (6秒以上): 諦めて安全着陸
                                state.add_log("ラインロスト: 復帰できず自動着陸します")
                                if not state.is_simulation and tello:
                                    try:
                                        tello.land()
                                    except Exception:
                                        pass
                                state.in_flight = False
                                yaw_speed = 0
                    else:
                        yaw_speed = 0

                # --- 3.7 Telloへの制御コマンド送信 ---
                if state.in_flight and not state.is_simulation and tello:
                    try:
                        tello.send_rc_control(0, forward_speed, up_down_speed, yaw_speed)
                    except Exception as e:
                        if state.lost_counter % 30 == 0:
                            state.add_log(f"RC送信エラー: {e}")

                # --- 3.8 状態の反映 ---
                with state.lock:
                    state.line_found = line_found
                    state.error = error
                    state.yaw_speed = yaw_speed
                    state.current_forward_speed = forward_speed if state.in_flight else 0

                # --- 3.9 オーバーレイ描画 ---
                if line_found:
                    cv2.circle(img, (cx, cy_global), 8, (0, 0, 255), -1)
                    cv2.line(img, (WIDTH // 2, HEIGHT), (cx, cy_global), (255, 0, 0), 2)

                cv2.line(img, (WIDTH // 2, 0), (WIDTH // 2, HEIGHT), (255, 255, 255), 1)
                cv2.line(img, (0, current_roi_start_y), (WIDTH, current_roi_start_y), (0, 255, 255), 1)

                cv2.putText(img, f"Battery: {state.battery}%", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
                cv2.putText(img, f"Status: {'FLIGHT' if state.in_flight else 'LANDED'}", (10, 50),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                            (0, 255, 0) if state.in_flight else (0, 0, 255), 1)
                cv2.putText(img, f"Error: {error}" if line_found else "Line Lost", (10, 70),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                            (0, 255, 0) if line_found else (0, 0, 255), 1)
                if state.is_simulation:
                    cv2.putText(img, "SIMULATION MODE", (10, 90),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 1)

                # --- 3.10 画像バッファへコピー ---
                thresh_color = cv2.cvtColor(thresh, cv2.COLOR_GRAY2BGR)
                with state.lock:
                    state.frame_camera = img.copy()
                    state.frame_thresh = thresh_color

                time.sleep(0.033)

            except Exception as e:
                # ★ 個別フレームのエラー → ログに出して次のフレームへ（ループは絶対に止めない）
                state.add_log(f"フレーム処理エラー: {e}")
                time.sleep(0.1)

    finally:
        print("ドローンループ終了処理を開始中...")
        if not state.is_simulation and tello:
            try:
                tello.send_rc_control(0, 0, 0, 0)
                if state.in_flight:
                    tello.land()
                tello.streamoff()
            except Exception:
                pass
        if webcam:
            webcam.release()
        print("ドローンループを終了しました。")


# --- メイン関数 ---
def main():
    import socket

    def find_free_port(start_port=5003, max_port=5013):
        for port in range(start_port, max_port):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(('127.0.0.1', port)) != 0:
                    return port
        return start_port

    port = find_free_port(5003)

    # ドローン制御用スレッドの開始
    t = threading.Thread(target=drone_loop)
    t.daemon = True
    t.start()

    print("\n" + "=" * 50)
    print("  Tello Drone Line Tracer - Web Dashboard")
    print("  サーバーが起動しました。")
    print("  ブラウザで以下のアドレスにアクセスしてください:")
    print(f"  👉 http://127.0.0.1:{port}")
    print("  ※ 終了するにはターミナルで Ctrl+C を押してください")
    print("=" * 50 + "\n")

    try:
        app.run(host='0.0.0.0', port=port, debug=False)
    except KeyboardInterrupt:
        print("\nユーザーによる強制終了を検出しました。")
    finally:
        state.running = False
        t.join(timeout=2.0)
        print("プログラムを安全に終了しました。")


if __name__ == "__main__":
    main()
