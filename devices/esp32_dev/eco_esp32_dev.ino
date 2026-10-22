/*
 * local-ai-ecosystem — ESP32 (classic Dev Module) device agent, single file.
 * Connects to the Pi's MQTT broker, announces a Manifest, subscribes to its
 * cmd topic, emits received->accepted->executing->completed Events, and sends
 * a periodic heartbeat so the API keeps it "live".
 * Board: "ESP32 Dev Module" (classic ESP32, NOT S3).
 * Libraries: PubSubClient (Nick O'Leary), ArduinoJson v7 (Benoit Blanchon).
 */
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// ===================== FILL THESE IN =====================
const char* WIFI_SSID     = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* MQTT_BROKER   = "YOUR_PI_IP";   // the Pi
const int   MQTT_PORT     = 1883;
// =========================================================

static const char* DEVICE_ID = "esp32_dev_01"; // must match ^[a-z0-9_]{3,32}$
static const int   SCHEMA_VERSION = 1;
static const int   PROTOCOL_VERSION = 1;
static const int   LED_PIN = 2;                // onboard blue LED on classic ESP32 DevKit

String CMD_TOPIC, EVT_TOPIC, MANIFEST_TOPIC, STATE_TOPIC;
WiFiClient net;
PubSubClient mqtt(net);
uint32_t msgCounter = 0;

String T(const char* kind) { return String("eco/v1/devices/") + DEVICE_ID + "/" + kind; }
String newMsgId() { return String(DEVICE_ID) + "-" + String(millis()) + "-" + String(msgCounter++); }
uint64_t nowMs() { return (uint64_t)millis(); } // shortcut: boot-relative ts, no NTP yet

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

void publishManifest() {
  JsonDocument doc;
  doc["schema_version"]   = SCHEMA_VERSION;
  doc["msg_id"]           = newMsgId();
  doc["device_id"]        = DEVICE_ID;
  doc["ts"]               = nowMs();
  doc["device_type"]      = "esp32";
  doc["hw"]               = "esp32-devkit";
  doc["fw_version"]       = "0.1";
  doc["protocol_version"] = PROTOCOL_VERSION;
  JsonArray caps = doc["capabilities"].to<JsonArray>();
  JsonObject c1 = caps.add<JsonObject>();
  c1["name"] = "ping"; c1["params"].to<JsonObject>();
  c1["max_duration_ms"] = 1000; c1["physical"] = false; c1["risk"] = "none";
  JsonObject c2 = caps.add<JsonObject>();
  c2["name"] = "blink_led"; c2["params"].to<JsonObject>();
  c2["max_duration_ms"] = 5000; c2["physical"] = true; c2["risk"] = "low";
  char buf[512];
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

bool doAction(const char* action, JsonVariant params) {
  if (strcmp(action, "ping") == 0) return true;
  if (strcmp(action, "blink_led") == 0) {
    long ms = params["ms"] | 500;
    if (ms > 5000) ms = 5000;            // honor max_duration_ms
    digitalWrite(LED_PIN, HIGH); delay(ms); digitalWrite(LED_PIN, LOW);
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
  // shortcut: expiry enforced by the issuer (Pi/laptop), ESP has no wall clock yet
  publishEvent(commandId, "accepted");
  publishEvent(commandId, "executing");
  bool ok = doAction(action, doc["params"]);
  publishEvent(commandId, ok ? "completed" : "failed");
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
    if (mqtt.connect(cid.c_str(), nullptr, nullptr,
                     STATE_TOPIC.c_str(), 1, true, willBuf)) {
      Serial.println("MQTT connected");
      mqtt.subscribe(CMD_TOPIC.c_str(), 1);
      Serial.printf("subscribed %s\n", CMD_TOPIC.c_str());
      publishManifest();
      publishState(true);
    } else {
      Serial.printf("  failed rc=%d, retry in 2s\n", mqtt.state());
      delay(2000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  pinMode(LED_PIN, OUTPUT); digitalWrite(LED_PIN, LOW);
  CMD_TOPIC = T("cmd"); EVT_TOPIC = T("evt");
  MANIFEST_TOPIC = T("manifest"); STATE_TOPIC = T("state");
  connectWifi();
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setBufferSize(1024);          // commands can exceed the 256B default
  mqtt.setCallback(onMessage);
  connectMqtt();
  Serial.println("agent running");
}

unsigned long lastBeat = 0;

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWifi();
  if (!mqtt.connected()) connectMqtt();
  mqtt.loop();
  // Periodic heartbeat so the API keeps us live (stale after 15s idle).
  if (millis() - lastBeat > 5000) { publishState(true); lastBeat = millis(); }
}
