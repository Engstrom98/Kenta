#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/i2s_std.h"
#include "driver/gpio.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "lwip/sockets.h"
#include "mdns.h"

static const char *TAG = "kenta";

// WiFi
#define WIFI_SSID     CONFIG_WIFI_SSID
#define WIFI_PASS     CONFIG_WIFI_PASS

// Server — fallback IP from menuconfig, prefer mDNS resolution
#define SERVER_IP     CONFIG_SERVER_IP
#define SERVER_PORT   12345
#define MDNS_HOSTNAME "kenta"
#define MDNS_RESOLVE_RETRIES 5
#define MDNS_RESOLVE_TIMEOUT_MS 3000
#define MDNS_RETRY_DELAY_MS  1000

// I2S pins
#define PIN_SCK  32
#define PIN_WS   25
#define PIN_SD   33

// Button & LED pins
#define PIN_BUTTON  13
#define PIN_LED_R   4
#define PIN_LED_G   18
#define PIN_LED_B   19

// Audio
#define SAMPLE_RATE   16000
#define SAMPLE_BITS   I2S_DATA_BIT_WIDTH_32BIT
#define DMA_BUF_COUNT 4
#define DMA_BUF_LEN   256

// Read 256 samples per I2S read
#define PCM_FRAME_LEN 256

// Grace period after button release (microseconds)
#define WAIT_TIMEOUT_US (3 * 1000 * 1000)

// recv() timeout in PROCESSING state (seconds)
#define RECV_TIMEOUT_S  120

// Button debounce time (microseconds)
#define DEBOUNCE_US (30 * 1000)

// Error LED flash duration (microseconds)
#define ERROR_FLASH_US (500 * 1000)

// End marker expected by server
static const uint8_t END_MARKER[4] = {0xDE, 0xAD, 0xBE, 0xEF};

// State machine
typedef enum {
    STATE_IDLE,
    STATE_RECORDING,
    STATE_WAIT,
    STATE_PROCESSING,
} state_t;

static i2s_chan_handle_t rx_chan;
static SemaphoreHandle_t wifi_ready;

// Static buffers
static int32_t i2s_raw[PCM_FRAME_LEN];
static int16_t pcm_frame[PCM_FRAME_LEN];

// Resolved server IP (filled by mDNS or fallback)
static char resolved_ip[64] = {0};

// Button debounce state
static int64_t last_button_change = 0;
static bool debounced_state = false;  // false = not pressed (button is active low)

// ---------------------------------------------------------------------------
// Button
// ---------------------------------------------------------------------------
static void button_init(void)
{
    gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << PIN_BUTTON,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);
}

static bool button_pressed(void)
{
    bool raw = (gpio_get_level(PIN_BUTTON) == 0);  // active low
    int64_t now = esp_timer_get_time();

    if (raw != debounced_state) {
        if (last_button_change == 0) {
            last_button_change = now;
        } else if (now - last_button_change > DEBOUNCE_US) {
            debounced_state = raw;
            last_button_change = 0;
        }
    } else {
        last_button_change = 0;
    }

    return debounced_state;
}

// ---------------------------------------------------------------------------
// RGB LED
// ---------------------------------------------------------------------------
static void led_init(void)
{
    gpio_config_t cfg = {
        .pin_bit_mask = (1ULL << PIN_LED_R) | (1ULL << PIN_LED_G) | (1ULL << PIN_LED_B),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);
}

static void led_off(void)
{
    gpio_set_level(PIN_LED_R, 0);
    gpio_set_level(PIN_LED_G, 0);
    gpio_set_level(PIN_LED_B, 0);
}

static void led_solid_blue(void)
{
    gpio_set_level(PIN_LED_R, 0);
    gpio_set_level(PIN_LED_G, 0);
    gpio_set_level(PIN_LED_B, 1);
}

static void led_solid_green(void)
{
    gpio_set_level(PIN_LED_R, 0);
    gpio_set_level(PIN_LED_G, 1);
    gpio_set_level(PIN_LED_B, 0);
}

static void led_solid_red(void)
{
    gpio_set_level(PIN_LED_R, 1);
    gpio_set_level(PIN_LED_G, 0);
    gpio_set_level(PIN_LED_B, 0);
}

static void led_flash_red(void)
{
    led_solid_red();
    int64_t start = esp_timer_get_time();
    while (esp_timer_get_time() - start < ERROR_FLASH_US) {
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    led_off();
}

// ---------------------------------------------------------------------------
// WiFi
// ---------------------------------------------------------------------------
static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "WiFi disconnected, reconnecting...");
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "Connected! IP: " IPSTR, IP2STR(&event->ip_info.ip));
        xSemaphoreGive(wifi_ready);
    }
}

static void wifi_init(void)
{
    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL));

    wifi_config_t wifi_cfg = {
        .sta = {
            .ssid = WIFI_SSID,
            .password = WIFI_PASS,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
}

// ---------------------------------------------------------------------------
// mDNS hostname resolution
// ---------------------------------------------------------------------------
static bool resolve_mdns_hostname(void)
{
    esp_err_t err = mdns_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "mdns_init failed: %s", esp_err_to_name(err));
        return false;
    }

    for (int attempt = 0; attempt < MDNS_RESOLVE_RETRIES; attempt++) {
        ESP_LOGI(TAG, "Resolving %s.local (attempt %d/%d)...",
                 MDNS_HOSTNAME, attempt + 1, MDNS_RESOLVE_RETRIES);

        esp_ip4_addr_t addr;
        addr.addr = 0;
        err = mdns_query_a(MDNS_HOSTNAME, MDNS_RESOLVE_TIMEOUT_MS, &addr);
        if (err == ESP_OK) {
            snprintf(resolved_ip, sizeof(resolved_ip), IPSTR, IP2STR(&addr));
            ESP_LOGI(TAG, "Resolved %s.local -> %s", MDNS_HOSTNAME, resolved_ip);
            return true;
        }
        ESP_LOGW(TAG, "mDNS query failed: %s, retrying...", esp_err_to_name(err));
        vTaskDelay(pdMS_TO_TICKS(MDNS_RETRY_DELAY_MS));
    }

    return false;
}

// ---------------------------------------------------------------------------
// I2S
// ---------------------------------------------------------------------------
static void i2s_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = DMA_BUF_COUNT;
    chan_cfg.dma_frame_num = DMA_BUF_LEN;
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, NULL, &rx_chan));

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(SAMPLE_BITS, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = PIN_SCK,
            .ws = PIN_WS,
            .dout = I2S_GPIO_UNUSED,
            .din = PIN_SD,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };
    std_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;

    ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx_chan, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(rx_chan));
    ESP_LOGI(TAG, "I2S initialized");
}

// ---------------------------------------------------------------------------
// Read one PCM frame from I2S (256 samples), convert to 16-bit
// Returns true on success, false on error.
// ---------------------------------------------------------------------------
static bool read_i2s_pcm(void)
{
    size_t bytes_read;
    int samples_got = 0;

    while (samples_got < PCM_FRAME_LEN) {
        int need = PCM_FRAME_LEN - samples_got;
        esp_err_t err = i2s_channel_read(rx_chan, i2s_raw + samples_got,
                         need * sizeof(int32_t), &bytes_read, portMAX_DELAY);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "i2s_channel_read error: %s", esp_err_to_name(err));
            return false;
        }
        samples_got += bytes_read / sizeof(int32_t);
    }

    for (int i = 0; i < PCM_FRAME_LEN; i++) {
        pcm_frame[i] = (int16_t)(i2s_raw[i] >> 16);
    }
    return true;
}

// ---------------------------------------------------------------------------
// TCP
// ---------------------------------------------------------------------------
static int tcp_connect(void)
{
    struct sockaddr_in dest = {
        .sin_family = AF_INET,
        .sin_port = htons(SERVER_PORT),
    };
    inet_pton(AF_INET, resolved_ip, &dest.sin_addr);

    int sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) {
        ESP_LOGE(TAG, "Socket creation failed");
        return -1;
    }

    if (connect(sock, (struct sockaddr *)&dest, sizeof(dest)) != 0) {
        ESP_LOGE(TAG, "TCP connect to %s:%d failed", resolved_ip, SERVER_PORT);
        close(sock);
        return -1;
    }

    ESP_LOGI(TAG, "Connected to server");
    return sock;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
void app_main(void)
{
    wifi_ready = xSemaphoreCreateBinary();

    wifi_init();
    i2s_init();
    button_init();
    led_init();
    led_off();

    // Wait for WiFi
    ESP_LOGI(TAG, "Waiting for WiFi...");
    xSemaphoreTake(wifi_ready, portMAX_DELAY);

    // Resolve server via mDNS, fall back to config IP
    if (!resolve_mdns_hostname()) {
        ESP_LOGW(TAG, "mDNS resolution failed, falling back to %s", SERVER_IP);
        strncpy(resolved_ip, SERVER_IP, sizeof(resolved_ip) - 1);
        resolved_ip[sizeof(resolved_ip) - 1] = '\0';
    }

    // Discard startup I2S samples
    for (int i = 0; i < 8; i++) {
        read_i2s_pcm();
    }

    ESP_LOGI(TAG, "Ready — press button to talk");

    state_t state = STATE_IDLE;
    int sock = -1;
    int64_t wait_start = 0;
    int64_t process_start = 0;
    bool led_on = false;
    int64_t last_blink = 0;

    while (1) {
        switch (state) {

        // ==== IDLE: wait for button press ====
        case STATE_IDLE:
            if (button_pressed()) {
                sock = tcp_connect();
                if (sock < 0) {
                    led_flash_red();
                    // Wait for button release before retrying
                    while (button_pressed()) {
                        vTaskDelay(pdMS_TO_TICKS(50));
                    }
                    break;
                }
                led_solid_blue();
                state = STATE_RECORDING;
                ESP_LOGI(TAG, "Recording...");
            } else {
                vTaskDelay(pdMS_TO_TICKS(20));
            }
            break;

        // ==== RECORDING: stream audio while button is held ====
        case STATE_RECORDING:
            if (!read_i2s_pcm()) {
                ESP_LOGW(TAG, "I2S read error, retrying...");
                break;
            }
            if (send(sock, pcm_frame, PCM_FRAME_LEN * sizeof(int16_t), 0) < 0) {
                ESP_LOGE(TAG, "send() failed, aborting");
                close(sock);
                sock = -1;
                led_flash_red();
                state = STATE_IDLE;
                break;
            }

            if (!button_pressed()) {
                // Button released — enter WAIT state
                wait_start = esp_timer_get_time();
                last_blink = wait_start;
                led_on = true;  // start with LED on (already blue)
                state = STATE_WAIT;
                ESP_LOGI(TAG, "Button released, waiting 3s...");
            }
            break;

        // ==== WAIT: 3s grace period, blink blue ====
        case STATE_WAIT: {
            int64_t now = esp_timer_get_time();

            // Blink blue at ~1.5 Hz (toggle every 333ms)
            if (now - last_blink > 333 * 1000) {
                led_on = !led_on;
                gpio_set_level(PIN_LED_B, led_on ? 1 : 0);
                last_blink = now;
            }

            if (button_pressed()) {
                // Resume recording
                led_solid_blue();
                state = STATE_RECORDING;
                ESP_LOGI(TAG, "Button pressed again, resuming recording...");
                break;
            }

            if (now - wait_start > WAIT_TIMEOUT_US) {
                // Grace period expired — send end marker and wait for server
                ESP_LOGI(TAG, "Grace period expired, processing...");
                if (send(sock, END_MARKER, sizeof(END_MARKER), 0) < 0) {
                    ESP_LOGE(TAG, "send() end marker failed");
                    close(sock);
                    sock = -1;
                    led_flash_red();
                    state = STATE_IDLE;
                    break;
                }

                // Switch to solid green
                led_solid_green();
                process_start = esp_timer_get_time();
                state = STATE_PROCESSING;
            } else {
                // Keep streaming audio during wait (captures trailing speech)
                if (!read_i2s_pcm()) {
                    ESP_LOGW(TAG, "I2S read error during wait, retrying...");
                    break;
                }
                if (send(sock, pcm_frame, PCM_FRAME_LEN * sizeof(int16_t), 0) < 0) {
                    ESP_LOGE(TAG, "send() failed during wait");
                    close(sock);
                    sock = -1;
                    led_flash_red();
                    state = STATE_IDLE;
                }
            }
            break;
        }

        // ==== PROCESSING: solid green, wait for server done byte ====
        case STATE_PROCESSING: {
            fd_set readfds;
            struct timeval tv;
            FD_ZERO(&readfds);
            FD_SET(sock, &readfds);
            tv.tv_sec = 0;
            tv.tv_usec = 100 * 1000;  // 100ms timeout for select

            int ret = select(sock + 1, &readfds, NULL, NULL, &tv);

            if (ret > 0 && FD_ISSET(sock, &readfds)) {
                uint8_t done_byte;
                int n = recv(sock, &done_byte, 1, 0);
                if (n == 1 && done_byte == 0x01) {
                    ESP_LOGI(TAG, "Server done, back to idle");
                } else {
                    ESP_LOGW(TAG, "Unexpected recv result (n=%d), returning to idle", n);
                }
                close(sock);
                sock = -1;
                led_off();
                state = STATE_IDLE;
                break;
            }

            if (ret < 0) {
                ESP_LOGE(TAG, "select() error, returning to idle");
                close(sock);
                sock = -1;
                led_flash_red();
                state = STATE_IDLE;
                break;
            }

            // Overall timeout check (use process_start, not wait_start)
            int64_t now = esp_timer_get_time();
            if (now - process_start > (int64_t)RECV_TIMEOUT_S * 1000 * 1000) {
                ESP_LOGW(TAG, "Processing timeout (%ds), returning to idle", RECV_TIMEOUT_S);
                close(sock);
                sock = -1;
                led_flash_red();
                state = STATE_IDLE;
                break;
            }
            break;
        }
        }
    }
}
