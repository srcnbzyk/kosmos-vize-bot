# Appointment Availability Checker

Kosmos Vize sisteminde sonraki N gun icin randevu musaitligini kontrol eder; bulursa Telegram'a bildirir.

## Onemli (2026 guncellemesi)

Canli frontend/API kontrolune gore:

- Endpoint ayni: `GetAppointmentHourQoutaInfo` (eski yazim: Qouta)
- Parametre isimleri ayni: `nationalityNumber`, `dealerId`, `date`, `appointmentTypeId`, `onlyAvailable`, `applicationType`
- **Yeni parametre:** `recaptchaToken`
- **Yeni auth:** endpoint `401` + `WWW-Authenticate: Bearer` donuyor; login sonrasi Bearer token gerekebilir
- `appointmentTypeId` degerleri: `16` STANDARD, `18` VIP, `2339` EEA_AB_SPOUSE, **yeni** `2472` BT
- `dealerId` listesi genisledi ama eski ID'ler duruyor (1 Istanbul, 5 Izmir, 1014 Ankara, 1017 Edirne, ...)

## Kurulum

```bash
pip install -r requirements.txt
cp .env.example .env
```

`.env` dosyasini kendi degerlerinle doldur.

## Calistirma

```bash
python bot.py
```

## Hatirlatma davranisi

Musait randevu bulununca Telegram'a bildirim baslar ve sen `Tamam.` yazana kadar devam eder:

1. Ilk **1 dakika**: her **10 saniye**
2. Sonraki **2 dakika**: her **30 saniye**
3. Sonraki **7 dakika**: her **1 dakika**
4. Sonrasinda da her **1 dakika** (durdurulana kadar)

Durdurmak icin botun yazdigi sohbete `Tamam` veya `Tamam.` yazman yeterli.

## .env alanlari

| Alan | Aciklama |
|------|----------|
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `TELEGRAM_CHAT_ID` | Bildirim chat ID |
| `NATIONALITY_NUMBER` | TC / nationalityNumber |
| `DEALER_ID` | Ofis ID (`/api/Dealers`) |
| `APPLICATION_TYPE` | `INDIVIDUAL` veya `FAMILY` |
| `APPOINTMENT_TYPE` | `STANDARD`, `VIP`, `EEA_AB_SPOUSE`, `BT` |
| `AUTH_TOKEN` | Opsiyonel Bearer token |
| `RECAPTCHA_TOKEN` | Opsiyonel reCAPTCHA token |
| `DAYS_AHEAD` | Kac gun ileri (varsayilan 30) |
| `CHECK_INTERVAL_SECONDS` | Dongu araligi (varsayilan 300 = 5 dk) |
