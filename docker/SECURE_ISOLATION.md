# إعداد Docker الآمن لعزل سيرفرات EVO HUB

هذا الدليل يشرح الحد الأدنى المقبول لتشغيل كود المستخدمين في بيئة منفصلة. **المجلد المنفصل أو البيئة الافتراضية `.venv` لا يساويان عزلًا عن النظام المضيف**؛ العزل الحقيقي يحتاج Container مستقلًا، مستخدمًا غير root، وصلاحيات Linux محدودة، وحدود موارد، وشبكة مضبوطة.

## 1. تجهيز المضيف

استخدم خادم Linux مخصصًا ومحدّثًا، ولا تشغّل لوحة Flask بحساب root. ثبّت Docker Engine من مستودع Docker الرسمي، ثم فعّل Docker rootless للمستخدم الذي يشغّل خدمة EVO HUB إن كان ذلك متاحًا في بيئتك. لا تضف مستخدم الويب إلى مجموعة `docker` في وضع rootful؛ عضوية هذه المجموعة تمنح صلاحية تعادل root على المضيف.

```bash
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

بعدها أنشئ حساب خدمة غير إداري، واجعل ملفات المشروع مملوكة له، ولا تمنح التطبيق صلاحية قراءة `/var/run/docker.sock` مباشرة. الأفضل أن يتواصل Flask مع خدمة صغيرة ومحدودة الصلاحيات تقوم فقط بإنشاء وإيقاف الحاويات بعد التحقق من `server_id` واسم المجلد والمنفذ.

## 2. إعداد كل Container

يستخدم `docker/run_server.sh` الموجود خارج مساحة المستخدم حاوية منفصلة لكل سيرفر، ويقبل public ID رقميًا من ثمانية أرقام فقط. صورة Docker و`evo-entrypoint` تُبني من مجلد `docker/` الثابت، ولا تُنسخ ملفات المستخدم إلى الصورة. داخل الحاوية يُركّب مجلد `storage/instances/<public_id>` وحده بصلاحية كتابة؛ أما صورة التشغيل فـ read-only، ويعمل التطبيق كمستخدم غير root (`10001:10001`) مع `tmpfs` محدود لـ`/tmp`، وإسقاط capabilities، و`no-new-privileges`، وSeccomp/AppArmor، وحدود CPU وRAM وعدد العمليات والملفات المفتوحة.

```bash
chmod 0555 docker/run_server.sh docker/evo-entrypoint
EVO_HUB_DOCKER_ENABLED=1 EVO_HUB_ALLOW_EGRESS=0 docker/run_server.sh 09156854 /srv/evo-hub/instances/09156854 13506 python
```

لا تستخدم `--privileged`، ولا تضف `--cap-add=SYS_ADMIN`، ولا تركّب Docker socket داخل الحاوية، ولا تركّب مجلدات المضيف الحساسة مثل `/`, `/home`, `/etc` أو `/proc` بصلاحية كتابة. اجعل الربط على `127.0.0.1` فقط، ثم اجعل Nginx أو Caddy هو الواجهة العامة التي تنهي TLS وتحوّل الطلبات إلى المنفذ المحلي.

## 3. ثبات طبقة Docker

لا تمنح أي ملف مرفوع صلاحية الوصول إلى Docker socket أو مجلد المشروع أو قاعدة البيانات. لا تستخدم Dockerfile داخل مساحة المستخدم لبناء صورة التشغيل؛ الـ runner يستخدم Dockerfiles وentrypoint الموجودة خارج mount المستخدم. لذلك يستطيع المستخدم تعديل ملفات Server الخاصة به فقط، بينما لا يستطيع تثبيت Docker أو حذف الصورة أو تعديل entrypoint من داخل الحاوية. حماية المضيف نفسه تتطلب أن يكون حساب خدمة EVO HUB غير root وأن تكون صلاحيات مجلد `docker/` والصور مملوكة للإدارة فقط.

## 4. الشبكة والإنترنت

الإعداد الافتراضي في المشغّل هو شبكة Docker داخلية بلا خروج إلى الإنترنت. فعّل `EVO_HUB_ALLOW_EGRESS=1` فقط للسيرفرات التي تحتاج اتصالًا خارجيًا، مثل بوت Telegram، ويفضل بعد ذلك استخدام شبكة egress أو Proxy يسمح فقط بالنطاقات المطلوبة. لا تسمح للسيرفرات بالوصول إلى خدمات metadata أو الشبكات الداخلية أو قاعدة بيانات لوحة التحكم.

كل سيرفر له رابط منطقي مستقل مثل:

```text
https://example.com/server/09156854
```

يجب أن يمر هذا الرابط إلى Reverse Proxy، لا أن يكشف Docker daemon أو منافذ الحاويات مباشرة على الإنترنت. في التطبيق الحالي يعرض المسار صفحة EVO HUB عندما يكون السيرفر متوقفًا، ويمرر الطلب إلى منفذ السيرفر عند تشغيله.

## 5. الموارد والاستمرارية

ضع حدودًا منفصلة لكل خطة. مثال مناسب كبداية هو `512m` RAM و`0.50` CPU و`128` process للحساب المجاني، مع قيم أعلى للحساب المدفوع بعد مراجعة المخاطر. لا تعتمد على قياس الواجهة فقط؛ اجعل Docker نفسه يفرض الحدود عبر `--memory`, `--cpus`, `--pids-limit`, و`--ulimit`.

استخدم `--restart=unless-stopped` فقط بعد إضافة health checks ومراقبة السجلات، حتى لا تدخل الحاوية في حلقة إعادة تشغيل. احتفظ بسجل محدود الحجم، ونفّذ rotation، ولا تسمح بعرض أسرار البيئة داخل Console أو صفحة الملفات.

## 6. Terminal الآمنة

الـTerminal في لوحة EVO HUB يجب أن تنفذ الأوامر عبر `docker exec` داخل Container السيرفر، وليس عبر `subprocess` على مضيف Flask. إذا كان Docker غير متاح، يجب تعطيل وضع Terminal المخصص للكود غير الموثوق أو إبقاؤه في وضع التطوير فقط؛ حدود المسار و`venv` وحدها لا تمنع كودًا خبيثًا من استدعاء النظام المضيف.

لا تعرض Docker socket للـTerminal. لا تسمح للمستخدم بإرسال خيارات Docker أو اسم Container مباشرة. الخادم هو الذي يختار اسم الحاوية من `public_id` بعد التحقق منه، وهو الذي يفرض `--user`, `--network`, والحدود الأمنية.

## 7. Reverse Proxy مختصر

مثال Nginx عام؛ عدّل اسم النطاق والمنفذ، وأضف TLS عبر Certbot أو عبر مزودك. لا تضع كلمة مرور أو مفتاحًا سريًا داخل ملف المشروع.

```nginx
server {
    listen 443 ssl http2;
    server_name example.com;

    location / {
        proxy_pass http://127.0.0.1:13506;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 60s;
    }
}
```

في الإنتاج، اجعل لوحة التحكم نفسها على نطاق منفصل أو مسار محمي، وأضف rate limiting وCSRF protection وSecure/HttpOnly cookies وHSTS، ولا تجعل صفحة حالة السيرفر تكشف مسارات الملفات أو رسائل أخطاء Python التفصيلية.

## 8. قائمة تحقق قبل الإنتاج

- Docker يعمل بإعداد rootless أو خدمة تشغيل مقيدة، ولا يوجد Docker socket داخل Flask أو داخل أي Container.
- كل سيرفر له Container وشبكة واسم public ID مستقل، ولا توجد mounts حساسة من المضيف.
- كل Container يعمل كمستخدم غير root مع `cap-drop=ALL` و`no-new-privileges` وSeccomp/AppArmor.
- الشبكة الداخلية مغلقة افتراضيًا، والخروج الخارجي مفعّل فقط عند الضرورة.
- المنافذ مكشوفة على loopback فقط خلف Reverse Proxy مع TLS.
- Terminal تنفذ داخل Container فقط في الإنتاج، مع مهلة وسقف مخرجات وسجل تدقيق محدود.
- رفع الملفات وفك ZIP يتحققان من المسارات ويمنعان symlink traversal وpath traversal.
- الأسرار مثل `BOT_TOKEN` تحفظ في Secret Manager أو متغيرات بيئة ولا توجد داخل Python أو ZIP أو قاعدة البيانات.
- توجد نسخ احتياطية مشفرة، وتدوير للسجلات، ومراقبة لفشل الحاويات والموارد.

> لا توجد طبقة واحدة تضمن حماية مطلقة. Docker يقلل نطاق الاختراق، لكنه يحتاج تحديثًا مستمرًا، وإعدادًا صحيحًا، وReverse Proxy آمنًا، وعدم تشغيل خدمات غير موثوقة بصلاحيات عالية.

## ملاحظة عن بيئات kernel المقيدة

إذا كانت بيئة Docker لا توفر جدول iptables `raw`، يمكن تشغيل `EVO_HUB_DOCKER_HOST_NETWORK=1` كحل توافق مؤقت. هذا الخيار يغيّر عزل الشبكة فقط؛ ولا يركّب ملفات الموقع أو السيرفرات الأخرى، ولا يمنح الحاوية Docker socket، ولا يلغي non-root أو read-only rootfs أو mount الـ public ID. في خادم إنتاج كامل، يفضل استخدام شبكة Docker bridge أو internal العادية بدل host network.

في EVO HUB، يتم رفض تشغيل كود المستخدم إذا لم يكن Docker مفعّلًا، ولا يتم الرجوع إلى تشغيله على مضيف Flask تلقائيًا.
