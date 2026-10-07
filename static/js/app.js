// 状態保持
let activeFeed = 'camera'; // 'camera' or 'threshold'

// ページ読み込み完了時の処理
document.addEventListener('DOMContentLoaded', () => {
    initSSE();
    initMouseAssist();
});

// ビデオストリームの切り替え
function switchFeed(type) {
    if (activeFeed === type) return;
    
    activeFeed = type;
    const img = document.getElementById('video-stream');
    const desc = document.getElementById('feed-desc');
    
    // タブボタンのアクティブ表示切り替え
    const buttons = document.querySelectorAll('.tab-btn');
    buttons.forEach(btn => btn.classList.remove('active'));
    
    if (type === 'camera') {
        img.src = '/video_feed';
        buttons[0].classList.add('active');
        desc.innerText = "カメラのリアルタイム画像に、検出されたラインの輪郭（緑色）、重心（赤点）、および進むべき方向（青線）を描画しています。";
    } else {
        img.src = '/thresh_feed';
        buttons[1].classList.add('active');
        desc.innerText = "しきい値処理（二値化）を行った映像です。黒色のラインが白く抽出され、ノイズが除去されている様子を確認できます。";
    }
}

// Server-Sent Events (SSE) の初期化
function initSSE() {
    console.log("SSEの接続を開始します...");
    const eventSource = new EventSource('/status');

    eventSource.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            updateDashboard(data);
        } catch (e) {
            console.error("ステータスデータのパースに失敗しました:", e);
        }
    };

    eventSource.onerror = (err) => {
        console.error("SSE接続エラーが発生しました。再接続を試みます...", err);
        // エラー時は状態表示をオフラインに
        document.getElementById('mode-badge').innerText = "接続エラー";
        document.getElementById('mode-badge').className = "badge text-red";
    };
}

// ダッシュボードUIの更新
function updateDashboard(data) {
    // 動作モード
    const modeBadge = document.getElementById('mode-badge');
    if (data.is_simulation) {
        modeBadge.innerText = "シミュレーション";
        modeBadge.className = "badge simulation";
    } else {
        modeBadge.innerText = "ONLINE";
        modeBadge.className = "badge";
    }

    // バッテリー
    const batteryBar = document.getElementById('battery-bar');
    const batteryText = document.getElementById('battery-text');
    batteryBar.style.width = `${data.battery}%`;
    batteryText.innerText = `${data.battery}%`;
    
    // バッテリーの色調整 (低残量時に警告)
    if (data.battery < 20) {
        batteryBar.style.background = 'linear-gradient(to right, #ef4444, #f87171)';
    } else if (data.battery < 50) {
        batteryBar.style.background = 'linear-gradient(to right, #f59e0b, #fbbf24)';
    } else {
        batteryBar.style.background = 'linear-gradient(to right, #10b981, #34d399)';
    }

    // 飛行状態
    const flightStatus = document.getElementById('flight-status');
    if (data.in_flight) {
        flightStatus.innerText = "飛行中 (FLIGHT)";
        flightStatus.className = "status-val text-green";
    } else {
        flightStatus.innerText = "着陸済 (LANDED)";
        flightStatus.className = "status-val text-red";
    }

    // ライン検出状態
    const lineBadge = document.getElementById('line-detect-status');
    if (data.line_found) {
        lineBadge.innerText = "検出";
        lineBadge.className = "badge badge-found";
    } else {
        lineBadge.innerText = "ロスト";
        lineBadge.className = "badge badge-lost";
    }

    // 学習ステータス
    const learningStatus = document.getElementById('learning-status');
    if (learningStatus) {
        if (data.is_recording) {
            learningStatus.innerText = `🔴 記録中 (Lap ${data.lap_count})`;
            learningStatus.style.color = '#ef4444';
        } else if (data.lap_count > 1) {
            learningStatus.innerText = `🤖 予測学習中 (Lap ${data.lap_count})`;
            learningStatus.style.color = '#a855f7';
        } else {
            learningStatus.innerText = "未学習 (Lap 0)";
            learningStatus.style.color = '#9ca3af';
        }
    }

    // 現在の高度
    const currentHeightVal = document.getElementById('current-height-val');
    if (currentHeightVal) {
        currentHeightVal.innerText = `${data.current_height} cm`;
    }

    // システムログの更新
    const logContainer = document.getElementById('system-logs');
    if (logContainer && data.logs) {
        if (data.logs.length === 0) {
            logContainer.innerHTML = '<div style="color: #9ca3af;">ログ待機中...</div>';
        } else {
            // 新しいログのHTMLを生成
            const newHtml = data.logs.map(log => `<div>${log}</div>`).join('');
            
            // 内容が変わった時だけ更新し、一番下までスクロールする
            if (logContainer.innerHTML !== newHtml) {
                logContainer.innerHTML = newHtml;
                logContainer.scrollTop = logContainer.scrollHeight;
            }
        }
    }

    // エラー値
    const errorVal = document.getElementById('error-val');
    errorVal.innerText = data.error;
    if (data.line_found) {
        errorVal.className = "status-val text-green";
    } else {
        errorVal.className = "status-val text-red";
    }

    // エラー可視化ポインターの更新
    // 画像幅が 360 のため、エラー値の範囲は -180 から +180
    // これを 0% から 100% に変換
    const errorPointer = document.getElementById('error-pointer');
    let percent = 50; // デフォルト中心
    if (data.line_found) {
        const halfWidth = 180;
        const normalized = Math.max(Math.min(data.error, halfWidth), -halfWidth);
        percent = 50 + (normalized / halfWidth) * 50;
    }
    errorPointer.style.left = `${percent}%`;

    // 速度関係
    document.getElementById('stat-forward').innerText = data.forward_speed;
    document.getElementById('stat-yaw').innerText = data.yaw_speed;

    // パラメータ表示値の同期（ユーザーが操作していない時にサーバーの値と合わせる）
    // パラメータスライダーの更新（フォーカスされていない場合のみ）
    syncInputWithServer('input-kp', 'val-kp', data.params.kp);
    syncInputWithServer('input-kd', 'val-kd', data.params.kd);
    syncInputWithServer('input-forward-speed', 'val-forward-speed', data.params.forward_speed);
    syncInputWithServer('input-thres-val', 'val-thres-val', data.params.thres_val);
    syncInputWithServer('input-min-area', 'val-min-area', data.params.min_area);
    syncInputWithServer('input-max-area', 'val-max-area', data.params.max_area);
    syncInputWithServer('input-roi-start', 'val-roi-start-pct', data.params.roi_start_pct);
    syncInputWithServer('input-target-height', 'val-target-height', data.params.target_height);
}

// 入力要素がフォーカスされていない場合のみサーバー値を同期
function syncInputWithServer(inputId, valId, serverVal) {
    const input = document.getElementById(inputId);
    const valDisplay = document.getElementById(valId);
    if (document.activeElement !== input) {
        input.value = serverVal;
        valDisplay.innerText = serverVal;
    }
}

// 制御パラメータの更新送信
function updateParam(name, value) {
    // 画面の数値表示を即時更新
    let displayVal = value;
    if (name === 'kp') {
        displayVal = parseFloat(value).toFixed(2);
    }
    const valDisplayId = `val-${name.replace(/_/g, '-')}`;
    const displayEl = document.getElementById(valDisplayId);
    if (displayEl) {
        displayEl.innerText = displayVal;
    }

    // サーバーへの送信
    fetch('/update_params', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: `param=${name}&value=${value}`
    })
    .then(response => response.json())
    .then(data => {
        if (!data.success) {
            console.error(`パラメータ [${name}] の更新に失敗しました:`, data.message);
        }
    })
    .catch(err => {
        console.error("パラメータ更新APIの通信エラー:", err);
    });
}

// コマンド送信 (離陸・着陸・緊急)
function sendCommand(action) {
    console.log(`コマンド送信: ${action}`);
    
    // UI上のボタン状態の一時変更などの演出
    const btnId = `btn-${action}`;
    const btn = document.getElementById(btnId);
    if (btn) {
        const origText = btn.innerHTML;
        btn.innerHTML = `<span class="icon">⌛</span> 送信中...`;
        btn.disabled = true;
        
        setTimeout(() => {
            btn.innerHTML = origText;
            btn.disabled = false;
        }, 1500);
    }

    fetch('/control', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: `action=${action}`
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            console.log(`コマンド [${action}] 実行成功:`, data.message);
        } else {
            alert(`コマンド実行エラー: ${data.message}`);
        }
    })
    .catch(err => {
        console.error("コマンド送信APIの通信エラー:", err);
    });
}

// --- マウスアシスト制御 ---
let isDragging = false;
let lastSendTime = 0;

function initMouseAssist() {
    const videoImg = document.getElementById('video-stream');
    
    videoImg.addEventListener('mousedown', (e) => {
        isDragging = true;
        sendMouseInput(e, videoImg, true);
    });

    videoImg.addEventListener('mousemove', (e) => {
        if (!isDragging) return;
        // 50msに1回だけ送信（負荷軽減）
        const now = Date.now();
        if (now - lastSendTime > 50) {
            sendMouseInput(e, videoImg, true);
            lastSendTime = now;
        }
    });

    const endDrag = () => {
        if (isDragging) {
            isDragging = false;
            fetch('/mouse_control', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: `active=false&dx=0&dy=0`
            });
        }
    };

    videoImg.addEventListener('mouseup', endDrag);
    videoImg.addEventListener('mouseleave', endDrag);
}

function sendMouseInput(e, img, active) {
    const rect = img.getBoundingClientRect();
    // 中心を原点とする座標系に変換
    const centerX = rect.width / 2;
    const centerY = rect.height / 2;
    
    // -100 ~ 100 の範囲に正規化
    let dx = ((e.clientX - rect.left) - centerX) / centerX * 100;
    let dy = ((e.clientY - rect.top) - centerY) / centerY * 100;
    
    // 値を制限
    dx = Math.max(-100, Math.min(100, dx));
    dy = Math.max(-100, Math.min(100, dy));
    
    fetch('/mouse_control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: `active=${active}&dx=${dx}&dy=${dy}`
    }).catch(err => console.error("マウス入力送信エラー:", err));
}
