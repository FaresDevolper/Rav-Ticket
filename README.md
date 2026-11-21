# بوت التمب فويس

## 1) تجهيز السيرفر
1. سوّ روم صوتي واحد باسم مثل "اضغط لإنشاء روم" داخل كاتيجوري خاصة بالرومات المؤقتة.
2. سوّ روم نصي للوج.
3. فعّل Developer Mode في ديسكورد وانسخ آيدي الروم الصوتي وآيدي روم اللوج.

## 2) إعدادات البوت في Developer Portal
- فعّل Privileged Intents: **Server Members Intent** و **Message Content Intent**
- صلاحيات البوت المطلوبة (أو Administrator للتبسيط):
  View Channels, Manage Channels, Manage Roles (Manage Permissions), Move Members,
  Connect, Send Messages, Manage Messages, Pin Messages, Read Message History, Set Voice Channel Status

## 3) الرفع على Render
- ارفع الملفات على GitHub (المجلد icons/ مهم)
- Render > New Web Service > Build: `pip install -r requirements.txt` > Start: `python main.py`
- Environment: TOKEN, CREATE_CHANNEL_ID, LOG_CHANNEL_ID, (CATEGORY_ID اختياري), PYTHON_VERSION=3.12.3
- UptimeRobot: ضع رابط الخدمة (HTTP monitor كل 5 دقائق)

أول تشغيل يرفع الأيقونات تلقائياً كإيموجي للتطبيق (20 أيقونة).

## الأوامر (بدون بادئة، تُكتب في شات الروم)
| الأمر | وظيفته |
|---|---|
| لوحة الروم | يعيد إرسال لوحة التحكم المثبتة (للمالك) |
| استلام الروم | استلام الملكية إذا المالك غير موجود في الروم |
| معلومات الروم | معلومات الروم (المالك، الأعضاء، القفل، المنطقة...) |
| إعادة ضبط الروم | يرجع الروم للوضع الافتراضي ويمسح الحظر والموثوقين |

## تغيير لون الأيقونات
عدّل ICON_COLOR في generate_icons.py ثم شغّله `python generate_icons.py`،
وارفع ICON_VERSION في main.py (من 1 إلى 2) وأعد الرفع.
