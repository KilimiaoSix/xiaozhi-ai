import serial
import time
import sys

def main():
    port = 'COM7'
    baud = 115200
    try:
        ser = serial.Serial(port, baud, timeout=1)
        print(f"Monitoring {port} at {baud} baud. Press Ctrl+C to stop.")
        with open('serial_log.txt', 'w', encoding='utf-8') as f:
            start_time = time.time()
            while time.time() - start_time < 30: # Monitor for 30 seconds
                if ser.in_waiting:
                    line = ser.readline().decode('utf-8', errors='ignore')
                    print(line, end='')
                    f.write(line)
                    f.flush()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
