# EVO Security Hardening

تمت إضافة طبقة حماية تطبيقية داخل `main.py` ونسخة Apache مختصرة وصحيحة في `.htaccess`. طبقة التطبيق تمنع طرق HTTP غير المطلوبة، ومحاولات traversal وNull Byte، والوصول إلى المسارات المخفية والحساسة، وتفرض حد رفع قدره 1 MB، وتضيف حدودًا أولية للمعدل، وترفض الطلبات العابرة للمواقع عند وجود Origin أو Referer غير مطابق، وتضيف رؤوس `X-Content-Type-Options` و`X-Frame-Options` و`Referrer-Policy` و`Permissions-Policy` و`HSTS` عند HTTPS.

يعمل تنفيذ كود المستخدم عبر Docker فقط. تم حذف `--ulimit nproc=128:128` من `docker/run_server.sh` لأن حد العمليات على مستوى المستخدم المضيف قد يمنع إنشاء أول خيط للتطبيق. بقيت حدود Docker الخاصة بـ CPU وRAM وPIDs والملفات المفتوحة، مع المستخدم غير الجذر وإسقاط الصلاحيات ونظام ملفات الجذر للقراءة فقط.

أُجري تنظيف الحسابات كترحيل لمرة واحدة عبر المفتاح `account_reset_v1`: حُذفت الحسابات الحالية والسيرفرات والتذاكر المرتبطة بها، وأُنشئ أو أُبقي حساب الإدارة الوحيد المطابق للبريد المكوّن في `ADMIN_EMAIL`، وهو حاليًا `${ADMIN_EMAIL}`. لن تُحذف الحسابات الجديدة عند إعادة التشغيل.

## التشغيل الآمن

على مضيف النشر الذي يحتوي Docker، استخدم متغيرات بيئية حقيقية مثل:

```bash
export SECRET_KEY='ضع قيمة عشوائية طويلة هنا'
export ADMIN_EMAIL='${ADMIN_EMAIL}'
export ADMIN_PASSWORD='ضع كلمة مرور قوية هنا'
export EVO_HUB_DOCKER_ENABLED=1
python3 main.py
```

لا تُفعّل `EVO_HUB_ALLOW_UNSAFE_HOST_EXECUTION`؛ فغياب Docker يجب أن يمنع تشغيل كود المستخدم بدل تشغيله مباشرة على المضيف. ملف `.htaccess` يُستخدم فقط عند وضع Apache أمام التطبيق، بينما تبقى حماية Flask فعالة حتى مع خادم WSGI أو reverse proxy آخر.
