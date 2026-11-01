// Capacete -> ATLAS | fw 0.3.1
// Abra ESTA pasta na Arduino IDE (Arquivo > Abrir > capacete.ino) e faça o upload.
// Wi-Fi e token ficam em segredos.h (mesma pasta).
#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <ESPmDNS.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include "driver/i2s.h"
#include "esp_wifi.h"
#include "segredos.h"

// ====================== CONFIGURE AQUI ======================
const char *DEVICE_ID = "capacete-01";

const char *SERVER_HOST = "atlas";               // procura atlas.local por mDNS
const char *SERVER_IP_FALLBACK = "192.168.0.3";   // usado se o mDNS falhar
const uint16_t SERVER_PORT = 8770;

// Ganho do mic: shift 14 = escala de 18 bits. Se a fala ficar fraca, tente 13.
#define MIC_SHIFT 14

// Display (max. 20 colunas x 4 linhas)
const int LCD_COLS = 16;
const int LCD_ROWS = 2;
const bool MOSTRAR_TRANSCRICAO = true;  // mostra "> o que o Whisper entendeu" antes da resposta
// ============================================================

// ---------- Pinos ----------
const int PIN_BUZ1 = 23;
const int PIN_BUZ2 = 22;
const int PIN_I2S_SCK = 32;
const int PIN_I2S_WS = 25;
const int PIN_I2S_SD = 33;
const int PIN_SDA = 26;  // display
const int PIN_SCL = 27;  // display
const int PIN_BTN = 0;   // botao BOOT da placa
const int PIN_LED = 2;   // LED azul da placa (aceso = ATLAS escutando)

// ---------- Audio ----------
const int MIC_RATE = 16000;
const int BLOCK = 320;           // leitura do I2S: 20 ms
const int SEND_SAMPLES = 1600;   // envio: 100 ms por pacote (menos envios = loop livre)
static int32_t in32[BLOCK];
// 14 bytes reservados na frente: a lib monta o cabecalho WS ali e envia tudo numa escrita so
static uint8_t txBuf[WEBSOCKETS_MAX_HEADER_SIZE + SEND_SAMPLES * 2];
static int16_t *out16 = (int16_t *)(txBuf + WEBSOCKETS_MAX_HEADER_SIZE);
static int txN = 0;
int32_t dcAcc = 0;  // filtro passa-alta simples (tira o offset DC do INMP441)
bool micOk = false;

// ---------- Estado ----------
WebSocketsClient ws;
String wsPath;
bool wsOk = false;
bool atlasActive = false;
uint32_t wsLastOk = 0;
uint32_t lastResolve = 0;
uint32_t lastWifiTry = 0;

// ---------- Bipes (tocados sem travar o loop) ----------
struct Note {
  uint16_t f;
  uint16_t d;
};
const int MAX_NOTES = 24;
Note notes[MAX_NOTES];
int noteCount = 0, noteIdx = 0, buzSel = 2;  // 0 = buzzer 1, 1 = buzzer 2, 2 = os dois
bool toneActive = false;
uint32_t noteEnd = 0;

// ---------- Estatisticas ----------
uint32_t pktSent = 0, bytesSent = 0, statT0 = 0;
uint64_t levelSum = 0;
uint32_t levelN = 0;

// ===================== DISPLAY LCD (HD44780 + PCF8574) =====================
uint8_t lcdAddr = 0;
bool lcdOk = false;
const uint8_t LCD_BL = 0x08, LCD_EN = 0x04, LCD_RS = 0x01;
char lcdCache[4][21];

char msg[4][21];           // mensagem atual do ATLAS
uint32_t msgUntil = 0;     // ate quando mostrar a mensagem (0 = nenhuma)
bool msgFixa = false;      // ttl_ms = 0: fica ate o proximo quadro
bool backendDisplay = false;  // true depois que o backend mandar a 1a mensagem "display"
const char *estadoTxt = "Dormindo";

void pcfWrite(uint8_t v) {
  Wire.beginTransmission(lcdAddr);
  Wire.write(v);
  Wire.endTransmission();
}

void lcdNibble(uint8_t nib, bool rs) {  // so na inicializacao
  uint8_t d = (nib << 4) | LCD_BL | (rs ? LCD_RS : 0);
  pcfWrite(d | LCD_EN);
  delayMicroseconds(2);
  pcfWrite(d & ~LCD_EN);
  delayMicroseconds(60);
}

// Em funcionamento: 1 transacao I2C por byte (mais rapido, nao atrapalha o mic)
void lcdByte(uint8_t v, bool rs) {
  uint8_t ctl = LCD_BL | (rs ? LCD_RS : 0);
  uint8_t hi = (v & 0xF0) | ctl;
  uint8_t lo = ((v << 4) & 0xF0) | ctl;
  Wire.beginTransmission(lcdAddr);
  Wire.write(hi | LCD_EN);
  Wire.write(hi);
  Wire.write(lo | LCD_EN);
  Wire.write(lo);
  Wire.endTransmission();
}

void lcdCmd(uint8_t c) {
  lcdByte(c, false);
  if (c <= 0x03) delay(3);  // clear e home sao lentos
}

void lcdInit() {
  delay(50);
  lcdNibble(0x03, false);
  delay(5);
  lcdNibble(0x03, false);
  delay(5);
  lcdNibble(0x03, false);
  delayMicroseconds(150);
  lcdNibble(0x02, false);  // passa pra 4 bits
  lcdCmd(0x28);
  lcdCmd(0x0C);
  lcdCmd(0x01);
  lcdCmd(0x06);
}

void lcdSetCursor(int col, int row) {
  static const uint8_t off[] = {0x00, 0x40, 0x14, 0x54};
  lcdCmd(0x80 | (off[row] + col));
}

bool lcdDetectar() {
  for (uint8_t a = 0x20; a <= 0x3F; a++) {
    if (a > 0x27 && a < 0x38) continue;  // faixas dos PCF8574 / PCF8574A
    if (a == 0x3C || a == 0x3D) continue;  // OLED
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      lcdAddr = a;
      return true;
    }
  }
  return false;
}

// Escreve uma linha inteira (completa com espacos). So mexe no I2C se mudou.
void lcdLine(int row, const char *s) {
  if (!lcdOk || row >= LCD_ROWS) return;
  char buf[21];
  int i = 0;
  for (; i < LCD_COLS && s[i]; i++) buf[i] = s[i];
  for (; i < LCD_COLS; i++) buf[i] = ' ';
  buf[LCD_COLS] = 0;
  if (!strcmp(buf, lcdCache[row])) return;
  strcpy(lcdCache[row], buf);
  lcdSetCursor(0, row);
  for (int c = 0; c < LCD_COLS; c++) lcdByte((uint8_t)buf[c], true);
}

// ---------- Texto: UTF-8 -> ASCII e quebra em linhas ----------
int foldAscii(const char *in, char *out, int maxLen) {
  int n = 0;
  while (*in && n < maxLen) {
    uint8_t c = (uint8_t)*in;
    if (c < 0x80) {
      out[n++] = (c >= 32 && c < 127) ? (char)c : ' ';
      in++;
      continue;
    }
    if (c == 0xC3 && in[1]) {
      uint8_t d = (uint8_t)in[1];
      char r = '?';
      if (d >= 0x80 && d <= 0x85) r = 'A';
      else if (d == 0x87) r = 'C';
      else if (d >= 0x88 && d <= 0x8B) r = 'E';
      else if (d >= 0x8C && d <= 0x8F) r = 'I';
      else if (d == 0x91) r = 'N';
      else if (d >= 0x92 && d <= 0x96) r = 'O';
      else if (d >= 0x99 && d <= 0x9C) r = 'U';
      else if (d >= 0xA0 && d <= 0xA5) r = 'a';
      else if (d == 0xA7) r = 'c';
      else if (d >= 0xA8 && d <= 0xAB) r = 'e';
      else if (d >= 0xAC && d <= 0xAF) r = 'i';
      else if (d == 0xB1) r = 'n';
      else if (d >= 0xB2 && d <= 0xB6) r = 'o';
      else if (d >= 0xB9 && d <= 0xBC) r = 'u';
      out[n++] = r;
      in += 2;
      continue;
    }
    if (c == 0xE2 && in[1] && in[2]) {  // aspas curvas, travessao, reticencias
      uint8_t b1 = (uint8_t)in[1], d = (uint8_t)in[2];
      if (b1 == 0x80) {
        if (d == 0x98 || d == 0x99) out[n++] = '\'';
        else if (d == 0x9C || d == 0x9D) out[n++] = '"';
        else if (d == 0x93 || d == 0x94) out[n++] = '-';
        else if (d == 0xA6) {
          for (int k = 0; k < 3 && n < maxLen; k++) out[n++] = '.';
        }
      }
      in += 3;
      continue;
    }
    int len = (c >= 0xF0) ? 4 : (c >= 0xE0) ? 3 : 2;  // emoji e outros: pula a sequencia
    for (int k = 0; k < len && *in; k++) in++;
  }
  out[n] = 0;
  return n;
}

// Quebra por palavras em LCD_ROWS x LCD_COLS; corta com "..." se estourar.
void quebrarParaDisplay(const char *texto, char linhas[][21]) {
  char t[160];
  foldAscii(texto, t, sizeof(t) - 1);
  for (int r = 0; r < 4; r++) linhas[r][0] = 0;
  int row = 0, col = 0;
  bool truncado = false;
  const char *p = t;
  while (*p) {
    while (*p == ' ') p++;
    if (!*p) break;
    int full = 0;
    while (p[full] && p[full] != ' ') full++;
    int wl = full > LCD_COLS ? LCD_COLS : full;
    int need = wl + (col > 0 ? 1 : 0);
    if (col + need > LCD_COLS) {
      row++;
      col = 0;
    }
    if (row >= LCD_ROWS) {
      truncado = true;
      break;
    }
    if (col > 0) linhas[row][col++] = ' ';
    memcpy(&linhas[row][col], p, wl);
    col += wl;
    linhas[row][col] = 0;
    p += full;
  }
  if (truncado) {
    int r = LCD_ROWS - 1;
    int len = strlen(linhas[r]);
    if (len > LCD_COLS - 3) len = LCD_COLS - 3;
    memcpy(&linhas[r][len], "...", 4);
  }
}

void mostrarTexto(const char *texto, uint32_t ttl) {
  quebrarParaDisplay(texto, msg);
  msgFixa = (ttl == 0);
  msgUntil = millis() + ttl;
}

// Tela padrao (quando nao ha mensagem): status da conexao e do ATLAS
void telaAtualizar() {
  if (!lcdOk) return;
  if (msgFixa || (msgUntil && (int32_t)(millis() - msgUntil) < 0)) {
    for (int r = 0; r < LCD_ROWS; r++) lcdLine(r, msg[r]);
    return;
  }
  msgUntil = 0;
  if (WiFi.status() != WL_CONNECTED) {
    lcdLine(0, "ATLAS offline");
    lcdLine(1, "Sem WiFi");
  } else if (!wsOk) {
    lcdLine(0, "ATLAS offline");
    lcdLine(1, "Conectando...");
  } else {
    lcdLine(0, "ATLAS online");
    lcdLine(1, estadoTxt);
  }
  for (int r = 2; r < LCD_ROWS; r++) lcdLine(r, "");
}

void telaBoot(const char *l0, const char *l1) {
  lcdLine(0, l0);
  lcdLine(1, l1);
}

// ===================== BUZZERS =====================
void setTone(int freq) {
  if (buzSel == 0 || buzSel == 2) ledcWriteTone(PIN_BUZ1, freq);
  if (buzSel == 1 || buzSel == 2) ledcWriteTone(PIN_BUZ2, freq);
}

void buzzersOff() {
  ledcWriteTone(PIN_BUZ1, 0);
  ledcWriteTone(PIN_BUZ2, 0);
}

void tonePump() {
  if (!toneActive) return;
  if ((int32_t)(millis() - noteEnd) < 0) return;
  if (noteIdx >= noteCount) {
    buzzersOff();
    toneActive = false;
    return;
  }
  Note n = notes[noteIdx++];
  setTone(n.f);  // freq 0 = pausa
  noteEnd = millis() + n.d;
}

// ===================== MICROFONE =====================
void micInit() {
  i2s_config_t cfg;
  memset(&cfg, 0, sizeof(cfg));
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  cfg.sample_rate = MIC_RATE;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT;  // L/R do mic no GND (testado: LEFT)
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = 32;  // 32 x 128 amostras = 256 ms de folga se o loop atrasar
  cfg.dma_buf_len = 128;   // 128 x 4 bytes = 512 bytes por buffer (limite: 1024)
  cfg.use_apll = false;

  i2s_pin_config_t pins;
  memset(&pins, 0, sizeof(pins));
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = PIN_I2S_SCK;
  pins.ws_io_num = PIN_I2S_WS;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = PIN_I2S_SD;

  if (i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL) != ESP_OK) {
    Serial.println("ERRO: i2s_driver_install");
    return;
  }
  if (i2s_set_pin(I2S_NUM_0, &pins) != ESP_OK) {
    Serial.println("ERRO: i2s_set_pin");
    return;
  }
  i2s_zero_dma_buffer(I2S_NUM_0);
  micOk = true;
}

void enviarPacote() {
  if (txN == 0) return;
  if (wsOk && ws.isConnected()) {
    if (ws.sendBIN(txBuf, txN * 2, true)) {  // true = cabecalho no espaco reservado
      pktSent++;
      bytesSent += txN * 2;
    }
  }
  txN = 0;
}

// Esvazia o que o DMA acumulou (ate ~160 ms por volta) e envia em pacotes de 100 ms.
void lerEnviarMic() {
  if (!micOk) return;
  for (int k = 0; k < 8; k++) {
    size_t bytes = 0;
    i2s_read(I2S_NUM_0, in32, sizeof(in32), &bytes, k == 0 ? 20 / portTICK_PERIOD_MS : 0);
    int n = bytes / 4;
    if (n <= 0) break;
    for (int i = 0; i < n; i++) {
      int32_t v = in32[i] >> MIC_SHIFT;
      dcAcc += v - (dcAcc >> 8);
      int32_t y = v - (dcAcc >> 8);
      if (y > 32767) y = 32767;
      if (y < -32768) y = -32768;
      out16[txN++] = (int16_t)y;
      levelSum += (y < 0 ? -y : y);
      if (txN == SEND_SAMPLES) enviarPacote();
    }
    levelN += n;
  }
}

// ===================== REDE =====================
// Potencia de TX menor: com sinal forte (-40..-60 dBm) nao faz falta, e evita a queda
// de tensao nos picos de transmissao (placa no USB + mic + LCD) que corrompe os pacotes.
// Sem economia de energia do radio (senao a latencia sobe para 100-300 ms).
void ajustarRadio() {
  WiFi.setTxPower(WIFI_POWER_8_5dBm);
  WiFi.setSleep(false);
  esp_wifi_set_ps(WIFI_PS_NONE);
}

void wifiConectar() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(DEVICE_ID);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);  // sem economia de energia do Wi-Fi: menos latencia
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("[wifi] conectando em '%s'", WIFI_SSID);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(300);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    ajustarRadio();
    Serial.printf("[wifi] ok, IP %s, sinal %d dBm\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  } else {
    Serial.println("[wifi] falhou (confira nome/senha e se a rede e 2,4 GHz). Vou tentar de novo.");
  }
}

void wifiManter() {
  static bool estavaConectado = false;
  if (WiFi.status() == WL_CONNECTED) {
    if (!estavaConectado) ajustarRadio();  // reconectou: reaplica (o driver pode resetar)
    estavaConectado = true;
    return;
  }
  estavaConectado = false;
  if (millis() - lastWifiTry < 5000) return;
  lastWifiTry = millis();
  Serial.println("[wifi] sem conexao, reconectando...");
  WiFi.reconnect();
}

IPAddress resolverServidor() {
  lastResolve = millis();
  for (int i = 0; i < 3; i++) {
    IPAddress ip = MDNS.queryHost(SERVER_HOST, 2000);
    if (ip != IPAddress(0, 0, 0, 0)) {
      Serial.printf("[mdns] %s.local = %s\n", SERVER_HOST, ip.toString().c_str());
      return ip;
    }
  }
  IPAddress fb;
  if (fb.fromString(SERVER_IP_FALLBACK)) {
    Serial.printf("[mdns] nao achou; usando IP fixo %s\n", fb.toString().c_str());
    return fb;
  }
  Serial.println("[mdns] nao achou o servidor e nao ha IP fixo configurado");
  return IPAddress(0, 0, 0, 0);
}

void iniciarWs() {
  IPAddress ip = resolverServidor();
  if (ip == IPAddress(0, 0, 0, 0)) return;
  ws.disconnect();
  ws.begin(ip, SERVER_PORT, wsPath.c_str());
  Serial.printf("[ws] conectando em %s:%u\n", ip.toString().c_str(), SERVER_PORT);
}

// ===================== MENSAGENS DO BACKEND =====================
void tratarJson(const char *txt, size_t len) {
  JsonDocument doc;
  if (deserializeJson(doc, txt, len)) return;
  const char *type = doc["type"] | "";

  if (!strcmp(type, "tone")) {
    JsonVariant b = doc["buzzer"];
    buzSel = b.is<int>() ? b.as<int>() : 2;  // "both" -> 2
    noteCount = 0;
    for (JsonVariant nv : doc["notes"].as<JsonArray>()) {
      if (noteCount >= MAX_NOTES) break;
      notes[noteCount].f = nv[0] | 0;
      notes[noteCount].d = nv[1] | 0;
      noteCount++;
    }
    buzzersOff();
    noteIdx = 0;
    noteEnd = millis();
    toneActive = noteCount > 0;
    Serial.printf("[bipe] %s (%d notas)\n", (const char *)(doc["name"] | "?"), noteCount);
  } else if (!strcmp(type, "state")) {
    atlasActive = doc["active"] | false;
    const char *st = doc["state"] | "";
    if (!strcmp(st, "idle")) estadoTxt = "Dormindo";
    else if (!strcmp(st, "listening")) estadoTxt = "Ouvindo...";
    else if (!strcmp(st, "thinking")) estadoTxt = "Pensando...";
    else if (!strcmp(st, "speaking")) estadoTxt = "Falando...";
    Serial.printf("[estado] %s | ativo=%d | dorme em %ds\n", st, (int)atlasActive, (int)(doc["expires_in"] | 0));
  } else if (!strcmp(type, "transcript")) {
    const char *t = doc["text"] | "";
    Serial.printf("[voce] %s\n", t);
    if (MOSTRAR_TRANSCRICAO) {
      char tmp[150];
      snprintf(tmp, sizeof(tmp), "> %s", t);
      mostrarTexto(tmp, 6000);  // a resposta substitui isso quando chegar
    }
  } else if (!strcmp(type, "reply")) {
    const char *t = doc["text"] | "";
    Serial.printf("[atlas] %s\n", t);
    if (!backendDisplay) mostrarTexto(t, 8000);  // fallback: backend ainda nao manda "display"
  } else if (!strcmp(type, "display")) {
    backendDisplay = true;
    const char *kind = doc["kind"] | "reply";
    uint32_t ttl = doc["ttl_ms"] | (strcmp(kind, "status") ? 8000 : 3000);
    JsonArray L = doc["lines"].as<JsonArray>();
    if (!L.isNull() && L.size() > 0) {
      for (int r = 0; r < 4; r++) msg[r][0] = 0;
      for (int r = 0; r < LCD_ROWS && r < (int)L.size(); r++) {
        const char *s = L[r].as<const char *>();
        foldAscii(s ? s : "", msg[r], LCD_COLS);
      }
      msgFixa = (ttl == 0);  // 0 = fica ate o proximo quadro
      msgUntil = millis() + ttl;
    } else {
      mostrarTexto(doc["text"] | "", ttl);
    }
    Serial.printf("[display/%s] %s\n", kind, (const char *)(doc["text"] | ""));
  } else if (!strcmp(type, "welcome")) {
    Serial.printf("[welcome] audio_target=%s | timeout=%ds\n", (const char *)(doc["audio_target"] | "?"),
                  (int)(doc["active_timeout_s"] | 0));
  } else if (!strcmp(type, "status")) {
    Serial.printf("[status] %s\n", (const char *)(doc["text"] | ""));
  } else if (!strcmp(type, "error")) {
    Serial.printf("[erro do servidor] %s\n", (const char *)(doc["error"] | ""));
  }
  // audio_start / audio_end: so com alto-falante (speaker=false por enquanto)
}

void enviarHello() {
  String h = "{\"type\":\"hello\",\"fw\":\"0.3.1\",\"mic_rate\":16000,\"speaker\":false,\"speaker_rate\":16000";
  if (lcdOk) h += String(",\"display\":{\"cols\":") + LCD_COLS + ",\"rows\":" + LCD_ROWS + ",\"ascii\":true}";
  h += "}";
  ws.sendTXT(h);
}

void onWsEvent(WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      wsOk = true;
      wsLastOk = millis();
      backendDisplay = false;
      txN = 0;
      Serial.println("[ws] CONECTADO");
      enviarHello();
      break;
    case WStype_DISCONNECTED:
      if (wsOk) Serial.println("[ws] desconectado");
      wsOk = false;
      atlasActive = false;
      msgFixa = false;
      estadoTxt = "Dormindo";
      break;
    case WStype_TEXT:
      tratarJson((const char *)payload, length);
      break;
    case WStype_ERROR:
      Serial.println("[ws] erro");
      break;
    default:
      break;
  }
}

// ===================== BOTAO =====================
void botaoLer() {
  static bool down = false, longSent = false;
  static uint32_t t0 = 0;
  bool pressed = digitalRead(PIN_BTN) == LOW;
  if (pressed && !down) {
    down = true;
    longSent = false;
    t0 = millis();
  } else if (pressed && down && !longSent && millis() - t0 >= 1000) {
    longSent = true;
    if (wsOk) ws.sendTXT("{\"type\":\"sleep\"}");
    Serial.println("[botao] sleep");
  } else if (!pressed && down) {
    down = false;
    if (!longSent && millis() - t0 > 30) {
      if (wsOk) ws.sendTXT("{\"type\":\"wake\"}");
      Serial.println("[botao] wake");
    }
  }
}

// ===================== STATS =====================
// Esperado com tudo certo: ~50 pacotes e ~156 KB a cada 5 s.
void imprimirStats() {
  if (millis() - statT0 < 5000) return;
  statT0 = millis();
  uint32_t nivel = levelN ? (uint32_t)(levelSum / levelN) : 0;
  Serial.printf("[stats] ws=%s | pacotes=%lu | %lu KB | mic nivel=%lu | RSSI=%d dBm\n", wsOk ? "ok" : "OFF",
                (unsigned long)pktSent, (unsigned long)(bytesSent / 1024), (unsigned long)nivel, WiFi.RSSI());
  pktSent = 0;
  bytesSent = 0;
  levelSum = 0;
  levelN = 0;
}

// ===================== SETUP / LOOP =====================
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== Capacete -> ATLAS | fw 0.3.1 ===");

  pinMode(PIN_BTN, INPUT_PULLUP);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);

  ledcAttach(PIN_BUZ1, 2000, 10);
  ledcAttach(PIN_BUZ2, 2000, 10);
  buzzersOff();

  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(100000);
  lcdOk = lcdDetectar();
  if (lcdOk) {
    lcdInit();
    Serial.printf("[lcd] ok em 0x%02X (%dx%d)\n", lcdAddr, LCD_COLS, LCD_ROWS);
    telaBoot("Capacete v0.3.1", "Iniciando...");
  } else {
    Serial.println("[lcd] nao encontrado (segue sem display)");
  }

  micInit();
  Serial.printf("[mic] %s\n", micOk ? "I2S ok" : "FALHOU");

  telaBoot("WiFi...", WIFI_SSID);
  wifiConectar();
  MDNS.begin(DEVICE_ID);

  wsPath = String("/ws/device?device_id=") + DEVICE_ID + "&token=" + HELMET_TOKEN;
  ws.onEvent(onWsEvent);
  ws.setReconnectInterval(2000);
  wsLastOk = millis();
  if (WiFi.status() == WL_CONNECTED) {
    telaBoot("Achando o PC...", "atlas.local");
    iniciarWs();
  }
  statT0 = millis();
}

void loop() {
  wifiManter();
  ws.loop();
  lerEnviarMic();
  tonePump();
  botaoLer();
  telaAtualizar();
  digitalWrite(PIN_LED, (wsOk && atlasActive) ? HIGH : LOW);

  // Servidor sumiu por mais de 20 s? Procura de novo (o IP do PC pode ter mudado).
  if (WiFi.status() == WL_CONNECTED && !wsOk && millis() - wsLastOk > 20000 && millis() - lastResolve > 20000) {
    iniciarWs();
  }
  imprimirStats();
}
