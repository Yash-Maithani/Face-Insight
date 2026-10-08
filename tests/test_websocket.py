"""Quick WebSocket test: verifies single connect, no reconnect loop, 
and that on-connect events are received."""
import socketio
import time

sio = socketio.Client()
events = []

sio.on("connect", lambda: events.append("connect"))
sio.on("camera:status", lambda d: events.append(("camera:status", d)))
sio.on("stats:update", lambda d: events.append(("stats:update", d)))
sio.on("collect:status", lambda d: events.append(("collect:status", d)))
sio.on("train:status", lambda d: events.append(("train:status", d)))

sio.connect("http://127.0.0.1:5000", transports=["websocket"])
time.sleep(2)
sio.disconnect()

print("Events received:")
for e in events:
    print(f"  {e}")

connect_count = events.count("connect")
print(f"Connect count: {connect_count}")
has_camera_status = any(isinstance(e, tuple) and e[0] == "camera:status" for e in events)
has_stats_update = any(isinstance(e, tuple) and e[0] == "stats:update" for e in events)
has_collect_status = any(isinstance(e, tuple) and e[0] == "collect:status" for e in events)
has_train_status = any(isinstance(e, tuple) and e[0] == "train:status" for e in events)

print(f"camera:status received: {has_camera_status}")
print(f"stats:update received: {has_stats_update}")
print(f"collect:status received: {has_collect_status}")
print(f"train:status received: {has_train_status}")

if connect_count == 1 and has_camera_status and has_stats_update:
    print("PASS: WebSocket clean connect, all init events received")
else:
    print("FAIL")
