#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);

// ====== CONFIG ======
const int N = 6;               // six gauges
const int CH_OFFSET = 1;       // PCA9685 channels 1..6 (channel 0 unused)

// Inputs
const int BTN_WATER = 2;       // momentary button to GND
const int SW_STAND  = 7;       // switch to GND when ON = STAND

// Servo pulse bounds (12-bit ticks out of 4096 at ~50Hz)
// Keep these "10° above zero and 10–15° above hardstop" style bounds.
// Tune per-gauge later if any needle hits the end.
int servoMin[N] = {140,140,140,140,140,140};
int servoMax[N] = {500,500,500,500,500,500};

const int STEP = 3;
const int STEP_DELAY_MS = 2;

int curPulse[N] = {320,320,320,320,320,320};

String line;

// ====== HELPERS ======
int clampInt(int x, int lo, int hi){ return x < lo ? lo : (x > hi ? hi : x); }

int valueToPulse(int i, int v0_100){
  v0_100 = clampInt(v0_100, 0, 100);
  long p = servoMin[i] + (long)(servoMax[i] - servoMin[i]) * v0_100 / 100L;
  return (int)p;
}

void writePulse(int pcaCh, int pulse){
  pwm.setPWM(pcaCh, 0, pulse);
}

void moveSmoothTo(int i, int target){
  target = clampInt(target, servoMin[i], servoMax[i]);
  int p = curPulse[i];
  if (p == target) return;
  int dir = (target > p) ? 1 : -1;

  while (p != target){
    p += dir * STEP;
    if ((dir > 0 && p > target) || (dir < 0 && p < target)) p = target;
    curPulse[i] = p;
    writePulse(i + CH_OFFSET, p);  // i=0..5 -> channels 1..6
    delay(STEP_DELAY_MS);
  }
}

// Expect: U,weather,temp,water,stand,event,commute
bool parseUpdate(const String &ln, int vals[N]){
  if (!ln.startsWith("U,")) return false;

  int start = 2; // after "U,"
  for (int i = 0; i < N; i++){
    int comma = ln.indexOf(',', start);
    String tok = (comma == -1) ? ln.substring(start) : ln.substring(start, comma);
    if (tok.length() == 0) return false;
    vals[i] = tok.toInt();
    if (comma == -1 && i != N - 1) return false;
    start = comma + 1;
  }
  return true;
}

// ====== INPUT EVENTS TO PC ======
bool lastWater = HIGH;
int lastStandState = -1; // -1 unknown, 0 sit, 1 stand

void setup() {
  Serial.begin(115200);

  pinMode(BTN_WATER, INPUT_PULLUP);
  pinMode(SW_STAND, INPUT_PULLUP);

  Wire.begin();
  pwm.begin();
  pwm.setPWMFreq(50);
  delay(10);

  // Initialize outputs to mid position on channels 1..6
  for (int i = 0; i < N; i++){
    curPulse[i] = (servoMin[i] + servoMax[i]) / 2;
    writePulse(i + CH_OFFSET, curPulse[i]);
  }

  // Send initial stand state
  bool standNow = (digitalRead(SW_STAND) == LOW); // switch to GND = stand
  lastStandState = standNow ? 1 : 0;

  Serial.println("GAUGE_PCA9685_READY");
  Serial.println("Expect: U,weather,temp,water,stand,event,commute");
  Serial.print("S,"); Serial.println(lastStandState);
}

void loop() {
  // WATER button edge -> PC
  bool wNow = digitalRead(BTN_WATER);
  if (lastWater == HIGH && wNow == LOW) Serial.println("B,WATER");
  lastWater = wNow;

  // STAND switch change -> PC
  bool standNow = (digitalRead(SW_STAND) == LOW);
  int standState = standNow ? 1 : 0;
  if (standState != lastStandState){
    lastStandState = standState;
    Serial.print("S,"); Serial.println(standState); // S,1 or S,0
  }

  // Serial receive -> move servos
  while (Serial.available()){
    char c = (char)Serial.read();
    if (c == '\n'){
      int vals[N];
      if (parseUpdate(line, vals)){
        for (int i = 0; i < N; i++){
          int target = valueToPulse(i, vals[i]);
          moveSmoothTo(i, target);
        }
      }
      line = "";
    } else if (c != '\r'){
      line += c;
      if (line.length() > 200) line = "";
    }
  }

  delay(2);
}
