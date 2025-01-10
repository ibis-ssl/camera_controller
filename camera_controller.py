import socket
import struct
from pythonosc import udp_client
from dataclasses import dataclass
import time
import json
import math
from typing import Tuple, Optional
import ssl_detection_pb2  # SSL-VisionのProtobufファイルから生成

@dataclass
class CameraConfig:
    """カメラの設定を保持するクラス"""
    # キャリブレーション用のパラメータ
    field_width: float = 6000  # フィールドの幅(mm)
    field_height: float = 4000  # フィールドの高さ(mm)
    pan_scale: float = 1.0  # パン方向のスケール調整
    tilt_scale: float = 1.0  # チルト方向のスケール調整
    
    # ズーム制御用のパラメータ
    min_zoom: int = 0
    max_zoom: int = 100
    min_distance: float = 1000  # この距離以下で最大ズーム
    max_distance: float = 5000  # この距離以上で最小ズーム

class BallTrackingCamera:
    def __init__(self, config: CameraConfig):
        self.config = config
        
        # UDP (SSL-Vision) の設定
        self.ssl_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ssl_socket.bind(('', 10006))  # SSL-Visionのデフォルトポート
        
        # OSCクライアントの設定
        self.osc_client = udp_client.SimpleUDPClient("127.0.0.1", 8000)
        
        # カメラの初期化
        self.initialize_camera()
        
    def initialize_camera(self):
        """カメラの初期化処理"""
        # カメラの接続
        self.osc_client.send_message("/OBSBOT/WebCam/General/Connected", 1)
        # デバイスの選択 (最初のデバイスを使用)
        self.osc_client.send_message("/OBSBOT/WebCam/General/SelectDevice", 0)
        # ジンバルのリセット
        self.osc_client.send_message("/OBSBOT/WebCam/General/ResetGimbal", 1)
        time.sleep(2)  # 初期化待ち
        
    def receive_ssl_frame(self) -> Optional[Tuple[float, float, float]]:
        """SSL-Visionからフレームを受信しボールの位置を返す"""
        try:
            data, _ = self.ssl_socket.recvfrom(2048)
            frame = ssl_detection_pb2.SSL_DetectionFrame()
            frame.ParseFromString(data)
            
            if frame.balls:
                # 最も確信度の高いボールを選択
                ball = max(frame.balls, key=lambda b: b.confidence)
                return ball.x, ball.y, ball.confidence
            return None
            
        except Exception as e:
            print(f"Error receiving SSL frame: {e}")
            return None
            
    def calculate_camera_angles(self, x: float, y: float) -> Tuple[float, float]:
        """ボールの位置からカメラの角度を計算"""
        # フィールド中心からの相対位置に変換
        rel_x = x / (self.config.field_width/2)  # -1.0 から 1.0 の範囲に正規化
        rel_y = y / (self.config.field_height/2)
        
        # カメラの角度を計算
        pan_angle = rel_x * 129 * self.config.pan_scale  # -129から129度の範囲
        tilt_angle = rel_y * 59 * self.config.tilt_scale  # -59から59度の範囲
        
        return max(-129, min(129, pan_angle)), max(-59, min(59, tilt_angle))
        
    def calculate_zoom(self, x: float, y: float) -> int:
        """ボールまでの距離からズーム値を計算"""
        distance = math.sqrt(x*x + y*y)
        
        if distance <= self.config.min_distance:
            return self.config.max_zoom
        elif distance >= self.config.max_distance:
            return self.config.min_zoom
            
        # 距離に応じて線形補間
        zoom_range = self.config.max_zoom - self.config.min_zoom
        distance_range = self.config.max_distance - self.config.min_distance
        zoom = self.config.max_zoom - (zoom_range * 
            (distance - self.config.min_distance) / distance_range)
        return int(max(self.config.min_zoom, min(self.config.max_zoom, zoom)))
        
    def move_camera(self, pan: float, tilt: float, zoom: int):
        """カメラを指定された位置に移動"""
        self.osc_client.send_message("/OBSBOT/WebCam/General/SetGimMotorDegree", 
                                   [90, int(pan), int(tilt)])
        self.osc_client.send_message("/OBSBOT/WebCam/General/SetZoom", zoom)
        
    def save_calibration(self, filename: str = "camera_config.json"):
        """キャリブレーション設定の保存"""
        with open(filename, 'w') as f:
            json.dump(vars(self.config), f, indent=4)
            
    def load_calibration(self, filename: str = "camera_config.json"):
        """キャリブレーション設定の読み込み"""
        try:
            with open(filename, 'r') as f:
                config_data = json.load(f)
                for key, value in config_data.items():
                    setattr(self.config, key, value)
        except FileNotFoundError:
            print(f"Calibration file {filename} not found. Using default values.")
            
    def calibration_mode(self):
        """キャリブレーションモード"""
        print("=== Calibration Mode ===")
        print("Commands:")
        print("  pan+ / pan- : Adjust pan scale")
        print("  tilt+ / tilt- : Adjust tilt scale")
        print("  save : Save calibration")
        print("  exit : Exit calibration mode")
        
        while True:
            cmd = input("> ").lower()
            if cmd == "pan+":
                self.config.pan_scale *= 1.1
            elif cmd == "pan-":
                self.config.pan_scale *= 0.9
            elif cmd == "tilt+":
                self.config.tilt_scale *= 1.1
            elif cmd == "tilt-":
                self.config.tilt_scale *= 0.9
            elif cmd == "save":
                self.save_calibration()
                print("Calibration saved")
            elif cmd == "exit":
                break
            else:
                print("Unknown command")
            
            print(f"Current scales - Pan: {self.config.pan_scale:.2f}, "
                  f"Tilt: {self.config.tilt_scale:.2f}")
            
    def run(self):
        """メインループ"""
        print("Starting ball tracking...")
        try:
            while True:
                ball_pos = self.receive_ssl_frame()
                if ball_pos:
                    x, y, confidence = ball_pos
                    if confidence > 0.5:  # 確信度が十分高い場合のみ追跡
                        pan, tilt = self.calculate_camera_angles(x, y)
                        zoom = self.calculate_zoom(x, y)
                        self.move_camera(pan, tilt, zoom)
                time.sleep(0.016)  # 約60Hz
                
        except KeyboardInterrupt:
            print("\nStopping ball tracking...")
            self.osc_client.send_message("/OBSBOT/WebCam/General/Disconnected", 1)

def main():
    # 初期設定
    config = CameraConfig()
    camera = BallTrackingCamera(config)
    
    # キャリブレーションファイルの読み込み
    camera.load_calibration()
    
    # コマンドライン引数でモード選択
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--calibrate":
        camera.calibration_mode()
    else:
        camera.run()

if __name__ == "__main__":
    main()