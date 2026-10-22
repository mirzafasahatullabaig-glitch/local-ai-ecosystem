/*
 * local-ai-ecosystem — ESP32 (classic Dev Module) device agent, single file.
 * MQTT device agent + peripherals: DHT11, SSD1306 OLED, buzzer.
 * Board: "ESP32 Dev Module" (classic ESP32, NOT S3).
 * Libraries (Tools > Manage Libraries):
 *   PubSubClient (Nick O'Leary), ArduinoJson v7 (Benoit Blanchon),
 *   DHT sensor library (Adafruit) + Adafruit Unified Sensor,
 *   Adafruit SSD1306 + Adafruit GFX.
 *
 * Wiring (common defaults — rewire or edit these to match your board):
 *   DHT11  data  -> GPIO 4   (+ 10k pullup data->3V3 recommended)
 *   Buzzer +     -> GPIO 5   (passive buzzer; - to GND)
 *   OLED   SDA   -> GPIO 21,  SCL -> GPIO 22,  VCC 3V3, GND GND
 *   Onboard LED  -> GPIO 2
 */
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <Wire.h>
#include <Adafruit_SSD1306.h>

// ===================== FILL THESE IN =====================
const char* WIFI_SSID     = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* MQTT_BROKER   = "YOUR_PI_IP";   // the Pi
const int   MQTT_PORT     = 1883;
const char* MQTT_USER     = "eco";                 // broker auth (user set on the Pi)
const char* MQTT_PASS     = "YOUR_BROKER_PASSWORD"; // fill in before flashing; keep out of git
// =========================================================

static const char* DEVICE_ID = "esp32_dev_01"; // must match ^[a-z0-9_]{3,32}$
static const int   SCHEMA_VERSION = 1;
static const int   PROTOCOL_VERSION = 1;

// --- pins (edit to match your wiring) ---
static const int LED_PIN   = 2;    // onboard blue LED
static const int DHT_PIN   = 4;    // DHT11 data
static const int BUZZ_PIN  = 5;    // passive buzzer
static const int OLED_SDA  = 21;   // I2C
static const int OLED_SCL  = 22;
static const uint8_t OLED_ADDR = 0x3C; // common SSD1306 address (some are 0x3D)

DHT dht(DHT_PIN, DHT11);
Adafruit_SSD1306 oled(128, 64, &Wire, -1);
bool oledOk = false;                   // false if the panel wasn't found -> skip drawing

String CMD_TOPIC, EVT_TOPIC, MANIFEST_TOPIC, STATE_TOPIC;
WiFiClient net;
PubSubClient mqtt(net);
uint32_t msgCounter = 0;

String T(const char* kind) { return String("eco/v1/devices/") + DEVICE_ID + "/" + kind; }
String newMsgId() { return String(DEVICE_ID) + "-" + String(millis()) + "-" + String(msgCounter++); }
uint64_t nowMs() { return (uint64_t)millis(); } // shortcut: boot-relative ts, no NTP yet

// --- OLED helper: show two lines of status; no-op if panel absent ---
void oledShow(const String& l1, const String& l2) {
  if (!oledOk) return;
  oled.clearDisplay();
  oled.setTextSize(1); oled.setTextColor(SSD1306_WHITE);
  oled.setCursor(0, 0);  oled.println(l1);
  oled.setCursor(0, 16); oled.println(l2);
  oled.display();
}

void publishEvent(const char* commandId, const char* state) {
  JsonDocument doc;
  doc["schema_version"] = SCHEMA_VERSION;
  doc["msg_id"]    = newMsgId();
  doc["device_id"] = DEVICE_ID;
  doc["ts"]        = nowMs();
  doc["command_id"]= commandId;
  doc["state"]     = state;
  char buf[256];
  size_t n = serializeJson(doc, buf);
  mqtt.publish(EVT_TOPIC.c_str(), (const uint8_t*)buf, n, false);
  Serial.printf("  -> evt %s\n", state);
}

// A result event: like an event but carries a small JSON payload (e.g. a reading).
void publishResult(const char* commandId, JsonDocument& payload) {
  payload["schema_version"] = SCHEMA_VERSION;
  payload["msg_id"]    = newMsgId();
  payload["device_id"] = DEVICE_ID;
  payload["ts"]        = nowMs();
  payload["command_id"]= commandId;
  payload["state"]     = "completed";
  char buf[256];
  size_t n = serializeJson(payload, buf);
  mqtt.publish(EVT_TOPIC.c_str(), (const uint8_t*)buf, n, false);
  Serial.println("  -> evt completed (+data)");
}

void publishManifest() {
  JsonDocument doc;
  doc["schema_version"]   = SCHEMA_VERSION;
  doc["msg_id"]           = newMsgId();
  doc["device_id"]        = DEVICE_ID;
  doc["ts"]               = nowMs();
  doc["device_type"]      = "esp32";
  doc["hw"]               = "esp32-devkit";
  doc["fw_version"]       = "0.2";  // bumped: added dht11/oled/buzzer
  doc["protocol_version"] = PROTOCOL_VERSION;
  JsonArray caps = doc["capabilities"].to<JsonArray>();
  auto addCap = [&](const char* name, int dur, bool phys, const char* risk) {
    JsonObject c = caps.add<JsonObject>();
    c["name"] = name; c["params"].to<JsonObject>();
    c["max_duration_ms"] = dur; c["physical"] = phys; c["risk"] = risk;
  };
  addCap("ping",      1000, false, "none");
  addCap("blink_led", 5000, true,  "low");
  addCap("read_dht",  1000, false, "none");   // temp/humidity
  addCap("beep",      3000, true,  "low");    // buzzer
  addCap("oled_show", 1000, true,  "none");   // write text to OLED
  char buf[640];
  size_t n = serializeJson(doc, buf);
  mqtt.publish(MANIFEST_TOPIC.c_str(), (const uint8_t*)buf, n, true); // retained
  Serial.println("published manifest");
}

void publishState(bool online) {
  JsonDocument doc;
  doc["online"] = online;
  doc["ts"]     = nowMs();
  char buf[96];
  size_t n = serializeJson(doc, buf);
  mqtt.publish(STATE_TOPIC.c_str(), (const uint8_t*)buf, n, true); // retained
}

// Returns true on success. For read_dht, fills `out` with temperature_c/humidity.
bool doAction(const char* action, JsonVariant params, JsonDocument& out) {
  if (strcmp(action, "ping") == 0) return true;

  if (strcmp(action, "blink_led") == 0) {
    long ms = params["ms"] | 500; if (ms > 5000) ms = 5000;
    digitalWrite(LED_PIN, HIGH); delay(ms); digitalWrite(LED_PIN, LOW);
    return true;
  }

  if (strcmp(action, "read_dht") == 0) {
    float t = dht.readTemperature();   // Celsius
    float h = dht.readHumidity();
    if (isnan(t) || isnan(h)) return false;   // sensor read failed
    out["temperature_c"] = t;
    out["humidity"] = h;
    oledShow("DHT11", String(t, 1) + "C  " + String(h, 0) + "%");
    return true;
  }

  if (strcmp(action, "beep") == 0) {
    // Active buzzer: drive the pin HIGH for the duration (built-in oscillator).
    // (A passive buzzer would need tone(); this board has an active one.)
    long ms = params["ms"] | 300; if (ms > 3000) ms = 3000;
    digitalWrite(BUZZ_PIN, HIGH); delay(ms); digitalWrite(BUZZ_PIN, LOW);
    return true;
  }

  if (strcmp(action, "oled_show") == 0) {
    const char* line1 = params["line1"] | "";
    const char* line2 = params["line2"] | "";
    oledShow(line1, line2);
    return true;
  }

  Serial.printf("  !! unknown action %s\n", action);
  return false;
}

void onMessage(char* topic, byte* payload, unsigned int len) {
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, payload, len);
  if (err) { Serial.printf("  !! bad command rejected: %s\n", err.c_str()); return; }
  const char* commandId = doc["command_id"] | "";
  const char* action    = doc["action"] | "";
  if (strlen(commandId) == 0) { Serial.println("  !! command missing command_id"); return; }
  Serial.printf("command %s: action=%s\n", commandId, action);
  publishEvent(commandId, "received");
  publishEvent(commandId, "accepted");
  publishEvent(commandId, "executing");

  JsonDocument result;   // read_dht fills this; others leave it empty
  bool ok = doAction(action, doc["params"], result);

  if (ok && !result.isNull()) publishResult(commandId, result);  // completed + data
  else publishEvent(commandId, ok ? "completed" : "failed");
}

void connectWifi() {
  Serial.printf("WiFi: connecting to %s\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.printf("\nWiFi up, IP %s\n", WiFi.localIP().toString().c_str());
}

void connectMqtt() {
  while (!mqtt.connected()) {
    Serial.printf("MQTT: connecting to %s:%d\n", MQTT_BROKER, MQTT_PORT);
    String cid = String("agent-") + DEVICE_ID;
    char willBuf[64];
    { JsonDocument w; w["online"] = false; serializeJson(w, willBuf); }
    if (mqtt.connect(cid.c_str(), MQTT_USER, MQTT_PASS,
                     STATE_TOPIC.c_str(), 1, true, willBuf)) {
      Serial.println("MQTT connected");
      mqtt.subscribe(CMD_TOPIC.c_str(), 1);
      Serial.printf("subscribed %s\n", CMD_TOPIC.c_str());
      publishManifest();
      publishState(true);
      oledShow("eco: online", String(DEVICE_ID));
    } else {
      Serial.printf("  failed rc=%d, retry in 2s\n", mqtt.state());
      oledShow("MQTT fail", "rc=" + String(mqtt.state()));
      delay(2000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  pinMode(LED_PIN, OUTPUT); digitalWrite(LED_PIN, LOW);
  pinMode(BUZZ_PIN, OUTPUT);
  dht.begin();
  Wire.begin(OLED_SDA, OLED_SCL);
  oledOk = oled.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR);  // false if panel not found
  if (!oledOk) Serial.println("OLED not found (continuing without it)");
  oledShow("eco booting", "");

  CMD_TOPIC = T("cmd"); EVT_TOPIC = T("evt");
  MANIFEST_TOPIC = T("manifest"); STATE_TOPIC = T("state");
  connectWifi();
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setBufferSize(1024);
  mqtt.setCallback(onMessage);
  connectMqtt();
  Serial.println("agent running");
}

unsigned long lastBeat = 0;

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWifi();
  if (!mqtt.connected()) connectMqtt();
  mqtt.loop();
  if (millis() - lastBeat > 5000) { publishState(true); lastBeat = millis(); }
}
