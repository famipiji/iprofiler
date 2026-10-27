# Skrip Demo — Bahasa Melayu (Jabatan Arkib Negara)

Terjemahan skrip asal Johnson Chua, disesuaikan untuk penyampaian lisan secara langsung. Nada: profesional tetapi mesra, sesuai untuk demo di hadapan pegawai kerajaan.

---

## Pengenalan — Apa itu iProfiler AI

Assalamualaikum dan salam sejahtera. Sebelum kita masuk kepada demo, izinkan saya perkenalkan dahulu apa itu iProfiler.

iProfiler adalah sistem AI yang direka untuk membaca, memahami, dan menyusun dokumen secara automatik — sama ada surat rasmi, laporan, atau manuskrip lama sekalipun. Sistem ini menggunakan teknologi AI penglihatan, atau *vision AI*, untuk "membaca" dokumen seperti mana manusia membaca — cuma dalam masa yang jauh lebih pantas.

Apa yang membezakan iProfiler adalah keupayaannya mengesan dan memahami pelbagai bahasa dan skrip dalam satu sistem yang sama — dari Bahasa Melayu, Bahasa Inggeris, Bahasa Cina, sehinggalah tulisan Jawi pada manuskrip lama. Ini amat relevan untuk konteks kerja pengarkiban, di mana rekod-rekod datang dalam pelbagai bahasa dan format sepanjang zaman.

Selepas maklumat diekstrak, pengguna juga boleh berbual terus dengan dokumen tersebut melalui ruangan sembang — untuk bertanya soalan, mengesahkan medan, atau membuat pembetulan dengan mudah, tanpa perlu kemahiran teknikal.

Dan yang paling penting untuk konteks hari ini — sebarang pembetulan yang dibuat akan diingati oleh sistem, dan digunakan secara automatik pada dokumen akan datang yang serupa jenisnya. Sistem ini juga boleh disepadukan terus dengan platform pengurusan dokumen sedia ada, seperti iVault, untuk penyimpanan dan pengurusan rekod jangka panjang.

Dengan itu, jom kita lihat bagaimana ia berfungsi dalam demo hari ini.

---

**00:00**

Jadi, soalan yang perlu kita tanya adalah — selepas kita imbas dokumen, kita sebenarnya perlu mengindeks dokumen ini supaya ia boleh disimpan dalam sistem pengurusan dokumen anda.

Secara tradisinya, terdapat banyak cara untuk kita lakukan ini. Tetapi hari ini, kami ingin kongsikan dengan pihak tuan-tuan dan puan-puan kaedah AI untuk melakukannya.

Perkara pertama yang membezakan kaedah AI berbanding kaedah konvensional adalah dari segi pembetulan, atau *corrections*, dan templat, atau *templates*.

Dari segi templat — untuk mengimbas satu dokumen baharu, sistem konvensional memerlukan pentadbir sistem atau pereka bentuk untuk mengkonfigurasi keseluruhan templat itu terlebih dahulu.

**00:00 (sambungan)**

Tanpa mengkonfigurasi templat — iaitu menetapkan semua medan yang diperlukan, dari mana medan itu hendak diambil, apakah corak *regex* yang digunakan, dan apakah peraturan-peraturannya, seperti nilai itu mesti sepanjang sekian-sekian aksara — tanpa semua itu disediakan dahulu, operator tidak boleh mengimbas, dan tidak boleh mengesahkan dokumen tersebut.

Peraturan kedua pula — katakan terdapat satu pembetulan yang perlu dibuat. Operator terpaksa membetulkannya setiap kali dokumen itu diproses, kerana sistem tidak akan mengingati apa yang telah dibetulkan sebelum ini. Sistem hanya akan menerima nilai baharu itu untuk dokumen tersebut sahaja.

Jadi, itulah dua masalah utama yang wujud dalam sistem OCR konvensional.

Apa yang kami tawarkan di sini adalah penyelesaian kepada kedua-dua masalah ini secara semula jadi, dalam sistem itu sendiri.

Perkara pertama — apa sahaja pembetulan yang kita ajar kepada AI, AI akan mengingatinya, dan secara automatik menggunakan pembetulan yang sama pada dokumen baharu bagi jenis dokumen yang serupa.

---

## Ciri tambahan — Pemisahan Dokumen Secara Pintar (AI Document Split)

Selalunya, bila kita ada satu fail PDF yang mengandungi beberapa jenis dokumen berbeza digabungkan sekali — contohnya invois, resit, dan surat rasmi dalam satu fail — cara biasa yang kita guna adalah alat seperti iLovePDF atau Adobe Acrobat untuk memisahkannya.

Tapi cara ini ada satu had yang besar — kita sendiri kena tahu di muka surat mana setiap dokumen bermula dan berakhir, dan kita kena asingkan satu-satu secara manual, muka surat demi muka surat.

Dengan iProfiler, proses ini berlaku secara automatik dan jauh lebih pintar. Sistem kami akan membaca setiap muka surat dalam PDF tersebut, mengenal pasti jenis rekod bagi setiap satu — sama ada ia invois, resit, atau jenis dokumen lain — dan secara automatik menentukan di mana satu dokumen tamat, dan di mana dokumen baharu bermula.

Yang lebih menarik, sistem ini bukan sekadar tengok jenis dokumen sahaja. Ia turut membandingkan kandungan sebenar — seperti nombor rujukan, tarikh, atau jumlah — untuk pastikan muka surat yang sepatutnya bersama tidak tersalah pisah. Contohnya, jika satu invois ada muka surat "Terma & Syarat" di penghujungnya yang formatnya kelihatan berbeza, sistem kami cukup pintar untuk kenal yang muka surat itu masih sebahagian daripada invois yang sama — bukan dokumen baharu yang berasingan.

Hasilnya, satu PDF yang mengandungi pelbagai jenis rekod akan terus dipisahkan kepada fail-fail berasingan mengikut jenis rekod sebenar — secara automatik, tanpa sebarang kerja manual. Ini menjimatkan masa yang banyak, terutamanya bila kita berurusan dengan bilangan dokumen yang besar, seperti dalam konteks pengarkiban rekod.

---

## Nota penyampaian

- "Corrections" dan "templates" dikekalkan dalam Bahasa Inggeris pada sebutan pertama (dengan padanan Melayu serta-merta selepasnya) — ini istilah teknikal yang biasa didengar dalam kalangan pegawai IT/rekod, jadi tak perlu dipaksa terjemah sepenuhnya.
- "Regex" turut dikekalkan sebagai istilah teknikal — boleh disebut terus "corak regex" tanpa perlu huraian panjang, kecuali jika pihak Arkib Negara nampak keliru.
- Jeda semula jadi dicadangkan selepas setiap perenggan pendek — gunakan sebagai tempat untuk tarik nafas atau tunjuk skrin semasa demo langsung.
- Jika mahu nada lebih santai/spontan (bukan skrip dibaca terus), boleh tukar "Jadi, soalan yang perlu kita tanya adalah" kepada "Jadi persoalannya di sini" — lebih pendek, lebih semula jadi bila bercakap.
- Segmen "Pemisahan Dokumen Secara Pintar" disahkan terus daripada kod sistem (`process_split_pdf` dalam `qwen_service.py`) — bukan andaian. Kalau nak demo langsung ciri ini, upload guna mod split "AI" semasa proses dokumen, bukan mod biasa.
