# Structure
GUI (Tkinter)
   ↓
Controller (logic điều phối)
   ↓
Service Layer
   ├── TikTokService (yt-dlp wrapper)
   ├── DownloadService
   ↓
Model (Video object)
   ↓
Utils (threading, cache, retry)

# Folder tree
tiktok_tool/
│
├── main.py
├── gui/
│   ├── app.py
│   ├── video_grid.py
│   ├── video_card.py
│   └── components.py
│
├── services/
│   ├── tiktok_service.py
│   ├── download_service.py
│
├── models/
│   └── video.py
│
├── utils/
│   ├── threading.py
│   ├── formatter.py
│   └── cache.py
│
└── assets/
    └── icons/

# Flow
User nhập link
   ↓
Controller.search()
   ↓
TikTokService.fetch_videos()
   ↓
Render VideoGrid
   ↓
User scroll / Load more
   ↓
Controller.load_more()
   ↓
Append UI
   ↓
User tick chọn video
   ↓
DownloadService.download_videos()