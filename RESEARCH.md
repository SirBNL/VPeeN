# مستند مهندسی معکوس افزونه VeePN (v5.0.2) و پروتکل بک‌اند

> تاریخ بررسی: ۲۰۲۶-۱۰-۰۷ — افزونه رسمی: `Free VPN for Chrome - VPN Proxy VeePN`
> ID: `majdfhpaihoncoakbjgbdhglocklcgno` — Manifest V3 — نسخه 5.0.2

## ۱. دو افزونه، دو دنیا

در جستجوی «veepn» در وب‌استور دو افزونه پیدا می‌شود:

| | «VeePN VPN» (جعلی) | «Free VPN for Chrome - VPN Proxy VeePN» (رسمی) |
|---|---|---|
| ID | `hdhkbojcompocgnbgpcaoocgnbnpojgl` | `majdfhpaihoncoakbjgbdhglocklcgno` |
| حجم | 49 KB | 2.4 MB (۵۴۹ فایل، ۵۱ زبان) |
| سرور پروکسی | `ge.zagryzkprd.space:443` هاردکد | `*.nexorcdn.com` با اعتبارنامه پویا از API |
| توضیحات | روسی، «premium» جعلی، دامنه `zagryzkprd.space` | برند رسمی veepn.com |
| مکانیزم | فقط `chrome.proxy` ثابت | PAC script + API چندمرحله‌ای |

افزونه جعلی در نصب، لندینگ‌پیج تبلیغاتی باز می‌کند و تمام ترافیک کاربر را از
سرور ناشناس خودش عبور می‌دهد. افزونه رسمی کاملاً متفاوت است.

## ۲. معماری افزونه رسمی

```
manifest.json → permissions: proxy, webRequest, webRequestAuthProvider ...
service-worker-loader.js → assets/background.ts-*.js (ارکستراسیون)
                          → assets/app-*.js        (سرویس‌ها: API, fetch, storage)
                          → assets/promo-banners-*.js (connectionController + connect())
                          → assets/schema.entity-*.js (zod-like schema ها)
```

### ۲.۱. مکانیزم ست کردن پروکسی در کروم

افزونه به‌جای `fixed_servers` از **PAC script** استفاده می‌کند:

```js
function FindProxyForURL(url, host) {
  // bypass: plain hosts, nonRoutableNets, exclusionList
  return 'HTTPS maasdam-10-nl.nexorcdn.com:58562';  // serverConfig های پیوست‌شده
}
```

اعتبارنامه‌ها از طریق `chrome.webRequest.onAuthRequired` (asyncBlocking) به‌صورت
`{authCredentials: {username, password}}` پاس داده می‌شوند. یعنی پروکسی
آپ‌استریم حتماً **username/password** دارد.

### ۲.۲. چرخش status اتصال

`disconnect → connecting → (server list) → (پروکسی ست) → (auth trigger) →
(checkInternetConnection با captive.apple.com / gstatic / firefox) → connected`
و در صورت خطا، سرور بعدی از لیست امتحان می‌شود.

## ۳. کشف دامنه‌های API (Domain Rotator)

افزونه دو «نوع دامنه» دارد: `free` و `premium`:

* پیش‌فرض free: `https://antpeak.com` — پیش‌فرض premium: `https://zorvian.com`
* سلام: `GET {domain}/v3/available/` → `{"message":"OK"}`
* لیست رزرو (کش ۲۴ ساعته؛ اگر TimeZone کاربر RU بود ترتیب باکت‌ها برعکس می‌شود):
  * `https://s3-oregon-1.s3-us-west-2.amazonaws.com/api.json`
  * `https://proigor.com/payload.json`
* شکل پاسخ: `{"free": url, "premium": url, "domains": {"free": [...], "premium": [...]}}`
* دامنه‌های free رزرو (اکتبر ۲۰۲۶): `hibchr.com`, `hisball.com`, `bitphox.com`,
  `freloop.com`, `tronlit.com`, `tronyza.com`
* دامنه‌های premium رزرو: `plus.hibchr.com`, `plus.hisball.com`, `codarka.com`,
  `premzon.com`, `qodami.com`, `techvaso.com`
* Backoff با فرمول `min(9e5 * 2^(failures-1), 72e5) * (0.5..1)`

## ۴. اندپوینت‌های API

هدرهای همه درخواست‌ها:

```
Accept: application/json
Content-Type: application/json
Authorization: Bearer <access>     (بجز available و launch)
```

### 4.1. `POST /v3/launch/` — ساخت نشست مهمان (بدون لاگین!)

```json
// body
{
  "udid": "26dfb9e5-2ee4-4f20-9cae-28cfea081ce7",     // UUID و ثبت‌شده در storage
  "appVersion": "5.0.2",
  "platform": "chrome",
  "platformVersion": "Mozilla/5.0 ... Chrome/130.0.0.0 ...",  // navigator.userAgent
  "timeZone": "Asia/Tehran",                          // Intl.DateTimeFormat
  "deviceName": "Chrome 130"                          // "<browser> <version>"
}
// response
{"access": "J0m_K67U7etKclWfyLSun5TAF95Onaxx_1793953142"}
```

* پاسخ در قالب پاکت `{success,data}` نیست — مستقیم است.
* پسوند عددی توکن یک timestamp است (~۳۰ روز اعتبار).
* اگر درخواست احراز هویت‌دار 401 بدهد، free-service دوباره `launch` می‌زند.

### 4.2. `GET /v3/location/extension/` — لیست لوکیشن‌ها

```json
{
  "categories": [{"id","name"}],
  "countries":  [{"id","name","code"}],
  "locations":  [{"id":"2.24","name":"Amsterdam","phrases":[...],
                  "region":"nl","countryCode":"NL",
                  "type":0,        // 0=public 1=private
                  "proxyType":0,   // 0=free  1=premium  ← کلید فیلتر رایگان
                  "icon_status":0,"icon_name":""}],
  "items": [ /* درخت categories>countries>locations برای UI */ ]
}
```

اکتبر ۲۰۲۶: **۱۸۵ لوکیشن** از که ۷ تای آن‌ها `proxyType=0` هستند:

| region | شهر |
|---|---|
| `nl` | Amsterdam |
| `fr-prs` | Paris |
| `gb-lnd` | London |
| `us-va` | Virginia |
| `us-or` | Oregon |
| `sg` | Singapore |
| `ru-spb` | Saint Petersburg |

### 4.3. `GET /v3/location/optimal/` — نزدیک‌ترین/بهترین لوکیشن

همان شکل یک لوکیشن + `weight`. برای نشست مهمان، رایگان برمی‌گردد (مثلاً `nl`).

### 4.4. `POST /v3/server/list/` — قلب تپنده! سرور + اعتبارنامه

```json
// body
{"protocol": "https", "region": "nl", "type": 0}
// response (آرایه)
[
  {
    "username": "5f7955346bf7b73e779c9fb04cd9bc06",
    "password": "cxVNXodSI25PHpRR",
    "region": "nl", "regionName": "Amsterdam",
    "regionDescription": "Netherlands, Amsterdam",
    "type": 0, "protocol": "https",
    "port": 58562, "rpz_port": 0,          // rpz_port برای حالت block-ads
    "addresses": ["maasdam-10-nl.nexorcdn.com"],
    "config": ""
  },
  ...
]
```

* `protocol` **فقط** `https` پذیرفته است؛ `socks5` → `422 {"name":"protocol",
  "message":"Protocol is invalid."}`
* اعتبارنامه‌ها per-launch صادر می‌شوند (بین ریکوئست‌های یک نشست ثابت‌اند ولی
  با launch جدید تغییر می‌کنند) و ظاهراً چند ساعت تا چند روز معتبرند.
* تعداد سرورها per-region: معمولاً ۳ عدد.

### 4.5. سایر اندپوینت‌ها (در افزونه تعریف شده‌اند)

```
/v3/token/             refresh توکن پرمیوم
/v3/login/  /v3/logout/  /v3/user/
/v3/registration/magic-token/
/v3/subscription/types/  /v3/subscription/trial-allowed/
/v3/setup/vpn/         اختصاص slot (اپ‌های دسکتاپ)
/v3/slot/types/  /v3/slot/release/
/v3/device/account-auth-token/
/v3/server/list/       همان لیست سرور (نقطه اشتراک اکستنشن و دسکتاپ)
/v3/config/remote/  /v3/config/popup/   remote-config و بنرها
/v3/url/review/chrome/  /v3/available/
```

## ۵. پروتکل آپ‌استریم (چیزی که واقعاً به آن وصل می‌شویم)

```
[TCP] → [TLS به سرور proxy:port]                        ← لایه اول TLS (گواهی *.nexorcdn.com)
CONNECT api.example.com:443 HTTP/1.1
Host: api.example.com:443
Proxy-Authorization: Basic base64(user:pass)
→ HTTP/1.1 200 Connection Established
[داخل تانل: ترافیک عادی کلاینت — برای HTTPS، لایه دوم TLS با مقصد]
```

یعنی «دابل TLS»: مثل هر پروکسی HTTPS استاندارد. افزونه کروم این را با PAC
`HTTPS host:port` ست می‌کند و کروم خودش TLS داخلی را مدیریت می‌کند.

**نتیجه تست‌ها (اکتبر ۲۰۲۶، از دیتاسنتر):**

```
$ curl -x "https://USER:PASS@maasdam-10-nl.nexorcdn.com:58562" http://api.ipify.org
108.181.122.11        # آمستردام

$ curl -x "https://USER:PASS@trout-east-4-us.nexorcdn.com:24202" https://api.ipify.org
15.204.166.125        # ویرجینیا
```

## ۶. محدودیت‌های مشاهده‌شده

* **Rate limit سخت‌گیرانه nginx** روی اندپوینت‌ها (429) — به‌ویژه
  `server/list`؛ حدوداً چند ده ثانیه فاصله امن است. به همین دلیل ابزار کش
  (توکن ~۷ روز، سرورها ۱۲ ساعت) + backoff نمایی + چرخش دامنه دارد.
* `available/` محدودیت کمتری دارد و برای probe خوب است.
* SOCKS5 آپ‌استریم وجود ندارد؛ فقط HTTPS CONNECT.

## ۷. آنچه veepn2sock روی همین اساس می‌سازد

1. توکن مهمان با UUID تصادفی (خودش را در state.json نگه می‌دارد)
2. کش دامنه‌ها از دو باکت + دامنه‌های هاردکد
3. کش سرورها per-region + refresh خودکار روی 401/403
4. بازنشر به‌صورت SOCKS5 (RFC1928، فقط CONNECT) و HTTP (CONNECT + absolute-form)
5. round-robin بین سرورهای یک region + تلاش سرور بعدی در خطا
6. ست/بازیابی پروکسی سیستم‌عامل با بکاپ تنظیمات قبلی
