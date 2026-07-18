from functools import lru_cache
import os
import re
from datetime import datetime

from . import api, media, util


def _sanitize_title(title: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", title).strip()


def _generate_name(created_at: datetime, index: int, title: str) -> str:
    dt = created_at
    if index == 0:
        episode = f"s{dt.year}e{dt.month:02d}{dt.day:02d}"
    else:
        episode = f"s{dt.year}e{dt.month:02d}{dt.day:02d}{index:02d}"
    safe_title = _sanitize_title(title)
    return f"{episode} - {safe_title}"


def _generate_filename(
    created_at: datetime, index: int, title: str, video_id: str
) -> str:
    name = _generate_name(created_at, index, title)
    return f"{name} [{video_id}].mp4"


def _generate_dirname(
    output_dir: str,
    channel_name: str,
    created_at: datetime,
    use_channel_dir: bool,
    use_season_dir: bool,
) -> str:
    directory = output_dir

    if use_channel_dir:
        directory = os.path.join(directory, channel_name)
    if use_season_dir:
        directory = os.path.join(directory, str(created_at.year))

    return directory


@lru_cache(maxsize=1)
def _list_videos_in_directory(directory: str) -> list[str]:
    videos = []
    if not os.path.exists(directory):
        return videos

    for filename in os.listdir(directory):
        if filename.endswith(".mp4"):
            videos.append(filename)

    return videos


def _find_local_filename(
    directory: str, video_id: str, post_id: str, is_single_video: bool
) -> str | None:
    for filename in _list_videos_in_directory(directory):
        if f"[{video_id}]" in filename:
            return filename

        if is_single_video and f"[{post_id}]" in filename:
            return filename

    return None


def _count_downloadable_items(
    post: api.Post,
    include_video: bool = True,
    include_audio: bool = False,
    include_images: bool = False,
) -> int:
    count = 0
    
    # Count videos
    if include_video:
        count += sum(
            1
            for v in post.get("data", [])
            if v.get("type") in ("ok_video", "video") 
            and v.get("complete") 
            and v.get("status") == "ok"
        )
    
    # Count audio files
    if include_audio:
        count += sum(
            1
            for v in post.get("data", [])
            if v.get("type") == "audio_file" and v.get("complete")
        )
    
    # Count images
    if include_images:
        count += sum(
            1
            for v in post.get("data", [])
            if v.get("type") == "image" and v.get("url")
        )
    
    return count


def _count_valid_videos(post: api.Post) -> int:
    return _count_downloadable_items(post, include_video=True, include_audio=False, include_images=False)


def _select_best_url(
    player_urls: list[api.PlayerUrl] | None, max_quality: str | None
) -> str | None:
    if not player_urls:
        return None

    available = [
        (item["type"], item["url"])
        for item in player_urls
        if item.get("type") in api.QUALITIES and item.get("url")
    ]

    if not available:
        return None

    available.sort(key=lambda x: api.QUALITIES.index(x[0]))

    if max_quality:
        max_idx = api.QUALITIES.index(max_quality)
        for quality, url in reversed(available):
            if api.QUALITIES.index(quality) <= max_idx:
                return url
        return None

    return available[-1][1]


def _get_audio_url(item: dict) -> str | None:
    """Get the download URL for an audio file."""
    return item.get("url") if item.get("type") == "audio_file" else None


def _get_image_url(item: dict) -> str | None:
    """Get the download URL for an image."""
    return item.get("url") if item.get("type") == "image" else None


def _get_file_extension(item: dict) -> str:
    """Get the appropriate file extension for an item."""
    item_type = item.get("type", "")
    
    if item_type == "ok_video" or item_type == "video":
        return ".mp4"
    elif item_type == "audio_file":
        # Try to get extension from title or use fileType
        title = item.get("title", "")
        if title and "." in title:
            return "." + title.rsplit(".", 1)[-1].lower()
        file_type = item.get("fileType", "").lower()
        if file_type == "mp3":
            return ".mp3"
        return ".mp3"  # default
    elif item_type == "image":
        # Try to determine image type from URL or default to jpg
        url = item.get("url", "")
        if ".png" in url.lower():
            return ".png"
        if ".webp" in url.lower():
            return ".webp"
        return ".jpg"
    return ""


def _extract_text_content(post: api.Post) -> str:
    """Extract and format text content from a post."""
    text_parts = []
    
    # Add post title
    title = post.get("title", "")
    if title:
        text_parts.append(f"# {title}\n")
    
    # Add post URL
    post_id = post.get("id", "")
    channel_name = post.get("user", {}).get("name", "")
    if post_id and channel_name:
        text_parts.append(f"Post URL: https://boosty.to/{channel_name}/posts/{post_id}\n")
    
    # Extract text from data items
    for item in post.get("data", []):
        if item.get("type") == "text":
            content = item.get("content", "")
            # Content is stored as JSON array: ["text", "style", []]
            if content and isinstance(content, list) and len(content) > 0:
                text_parts.append(content[0])
                text_parts.append("")
    
    return "\n".join(text_parts)


def _download_post_content(
    channel_name: str,
    output_dir: str,
    post: api.Post,
    max_quality: str | None = None,
    use_season_dir: bool = True,
    use_channel_dir: bool = True,
    update_metadata: bool = False,
    start_video_index: int = 0,
    include_video: bool = True,
    include_audio: bool = False,
    include_images: bool = False,
    include_text: bool = False,
    access_token: str | None = None,
) -> list[str]:
    post_id = post["id"]
    post_title = post["title"]
    created_at = datetime.fromtimestamp(post["createdAt"])
    post_url = f"https://boosty.to/{channel_name}/posts/{post_id}"
    post_artist = post.get("user", {}).get("name", channel_name)

    post_name = _generate_name(created_at, 0, post_title or post_id)
    downloaded_files = []

    if not post.get("hasAccess"):
        print(f"Skipping (no access): {post_name}")
        return downloaded_files

    # Count downloadable items
    item_count = _count_downloadable_items(
        post, include_video, include_audio, include_images
    )
    has_text_content = any(item.get("type") == "text" for item in post.get("data", []))
    
    if item_count == 0 and not (include_text and has_text_content):
        print(f"Skipping (no downloadable content): {post_name}")
        return downloaded_files

    # Save text content if requested
    if include_text and has_text_content:
        text_content = _extract_text_content(post)
        if text_content.strip():
            directory = _generate_dirname(
                output_dir,
                channel_name,
                created_at,
                use_channel_dir,
                use_season_dir,
            )
            os.makedirs(directory, exist_ok=True)
            
            text_filename = f"{_generate_name(created_at, 0, post_title or post_id)}.md"
            text_filepath = os.path.join(directory, text_filename)
            
            print(f"Saving text: {text_filename}")
            with open(text_filepath, "w", encoding="utf-8") as f:
                f.write(text_content)
            downloaded_files.append(text_filename)

    is_single_item = item_count == 1

    item_index = start_video_index

    for item in post.get("data", []):
        item_type = item.get("type")
        item_id = item.get("id")
        if not item_id and item_type != "text":
            continue  # Skip items without ID (except text which we already handled)
        
        # Handle videos
        if include_video and item_type in ("ok_video", "video"):
            if not item.get("complete") or item.get("status") != "ok":
                continue

            item_index += 1

            if is_single_item:
                item_title = post_title or item.get("title") or "untitled"
            else:
                item_title = item.get("title") or post_title or "untitled"

            item_name = _generate_name(created_at, item_index, item_title or item_id)
            preview_url = item.get("preview") or item.get("defaultPreview")

            url = _select_best_url(item.get("playerUrls"), max_quality)
            if not url:
                print(f"Skipping (no media): {item_name}")
                continue

            directory = _generate_dirname(
                output_dir,
                channel_name,
                created_at,
                use_channel_dir,
                use_season_dir,
            )

            local_filename = _find_local_filename(
                directory, item_id, post_id, is_single_item
            )

            if local_filename:
                filepath = os.path.join(directory, local_filename)
                if update_metadata:
                    print(f"Updating metadata: {local_filename}")
                    media.download_and_embed_metadata(
                        filepath, post_artist, item_title, preview_url, post_url
                    )
                else:
                    print(f"Skipping (exists): {local_filename}")
                continue

            if update_metadata:
                continue

            os.makedirs(directory, exist_ok=True)

            extension = _get_file_extension(item)
            filename = f"{_generate_name(created_at, item_index, item_title)}[{item_id}]{extension}"
            filepath = os.path.join(directory, filename)

            print(f"Downloading: {filename}")
            if media.download_file(filepath, url, access_token=access_token):
                # Embed metadata for MP4 files
                if extension == ".mp4":
                    print(f"Embedding metadata: {filename}")
                    media.download_and_embed_metadata(
                        filepath, post_artist, item_title, preview_url, post_url
                    )
                downloaded_files.append(filename)

        # Handle audio files
        elif include_audio and item_type == "audio_file":
            if not item.get("complete"):
                continue
            
            item_index += 1
            
            audio_title = item.get("title") or item.get("track") or post_title or "untitled"
            item_name = _generate_name(created_at, item_index, audio_title)
            
            url = _get_audio_url(item)
            if not url:
                print(f"Skipping (no url): {item_name}")
                continue

            directory = _generate_dirname(
                output_dir,
                channel_name,
                created_at,
                use_channel_dir,
                use_season_dir,
            )

            os.makedirs(directory, exist_ok=True)

            extension = _get_file_extension(item)
            filename = f"{_generate_name(created_at, item_index, audio_title)}[{item_id}]{extension}"
            filepath = os.path.join(directory, filename)

            if os.path.exists(filepath):
                print(f"Skipping (exists): {filename}")
                downloaded_files.append(filename)
                continue

            print(f"Downloading audio: {filename}")
            if media.download_file(filepath, url, access_token=access_token):
                downloaded_files.append(filename)

        # Handle images
        elif include_images and item_type == "image":
            url = _get_image_url(item)
            if not url:
                continue
            
            item_index += 1
            
            image_title = item.get("title") or post_title or "image"
            item_name = _generate_name(created_at, item_index, image_title)

            directory = _generate_dirname(
                output_dir,
                channel_name,
                created_at,
                use_channel_dir,
                use_season_dir,
            )

            os.makedirs(directory, exist_ok=True)

            extension = _get_file_extension(item)
            filename = f"{_generate_name(created_at, item_index, image_title)}[{item_id}]{extension}"
            filepath = os.path.join(directory, filename)

            if os.path.exists(filepath):
                print(f"Skipping (exists): {filename}")
                downloaded_files.append(filename)
                continue

            print(f"Downloading image: {filename}")
            if media.download_file(filepath, url, access_token=access_token):
                downloaded_files.append(filename)

    return downloaded_files


def _download_post_videos(
    channel_name: str,
    output_dir: str,
    post: api.Post,
    max_quality: str | None = None,
    use_season_dir: bool = True,
    use_channel_dir: bool = True,
    update_metadata: bool = False,
    start_video_index: int = 0,
    access_token: str | None = None,
) -> list[str]:
    """Legacy function for backward compatibility - only downloads videos."""
    return _download_post_content(
        channel_name=channel_name,
        output_dir=output_dir,
        post=post,
        max_quality=max_quality,
        use_season_dir=use_season_dir,
        use_channel_dir=use_channel_dir,
        update_metadata=update_metadata,
        start_video_index=start_video_index,
        include_video=True,
        include_audio=False,
        include_images=False,
        include_text=False,
        access_token=access_token,
    )


def download_post_videos(
    channel_name: str,
    post_id: str,
    output_dir: str,
    access_token: str | None = None,
    max_quality: str | None = None,
    use_season_dir: bool = True,
    use_channel_dir: bool = True,
    update_metadata: bool = False,
) -> list[str]:
    print(f"Fetching post {post_id} for channel {channel_name}...")
    post = api.get_post(channel_name, post_id, access_token)

    return _download_post_videos(
        channel_name,
        output_dir,
        post,
        max_quality,
        use_season_dir,
        use_channel_dir,
        update_metadata,
        0,
    )


def download_channel_videos(
    channel_name: str,
    output_dir: str,
    access_token: str | None = None,
    max_quality: str | None = None,
    days_back: int | None = None,
    use_season_dir: bool = True,
    use_channel_dir: bool = True,
    update_metadata: bool = False,
    include_video: bool = True,
    include_audio: bool = False,
    include_images: bool = False,
    include_text: bool = False,
) -> list[str]:
    suffix = f" (last {days_back} days)" if days_back is not None else ""
    print(f"Fetching posts for channel {channel_name}{suffix}...")
    posts = api.list_posts(channel_name, access_token, days_back)
    print(f"Found {len(posts)} posts for channel {channel_name}{suffix}")

    all_downloaded = []
    last_date = None
    item_index = 0

    for post in posts:
        created_at = datetime.fromtimestamp(post["createdAt"])
        current_date = created_at.date()

        if last_date != current_date:
            item_index = 0
            last_date = current_date

        downloaded = _download_post_content(
            channel_name,
            output_dir,
            post,
            max_quality,
            use_season_dir,
            use_channel_dir,
            update_metadata,
            item_index,
            include_video,
            include_audio,
            include_images,
            include_text,
            access_token,
        )
        all_downloaded.extend(downloaded)

        item_index += _count_downloadable_items(
            post, include_video, include_audio, include_images
        )

    return all_downloaded


def download_post_videos(
    channel_name: str,
    post_id: str,
    output_dir: str,
    access_token: str | None = None,
    max_quality: str | None = None,
    use_season_dir: bool = True,
    use_channel_dir: bool = True,
    update_metadata: bool = False,
) -> list[str]:
    print(f"Fetching post {post_id} for channel {channel_name}...")
    post = api.get_post(channel_name, post_id, access_token)

    return _download_post_videos(
        channel_name,
        output_dir,
        post,
        max_quality,
        use_season_dir,
        use_channel_dir,
        update_metadata,
        0,
        access_token,
    )


def download_links(
    links: list[str],
    output_dir: str,
    access_token: str | None = None,
    max_quality: str | None = None,
    days_back: int | None = None,
    use_season_dir: bool = True,
    use_channel_dir: bool = True,
    update_metadata: bool = False,
    include_video: bool = True,
    include_audio: bool = False,
    include_images: bool = False,
    include_text: bool = False,
) -> list[str]:
    all_downloaded = []
    for link in links:
        channel_name, post_id = util.parse_name_or_url(link)

        if post_id:
            downloaded = download_post_videos(
                channel_name=channel_name,
                post_id=post_id,
                output_dir=output_dir,
                access_token=access_token,
                max_quality=max_quality,
                use_season_dir=use_season_dir,
                use_channel_dir=use_channel_dir,
                update_metadata=update_metadata,
            )
        else:
            downloaded = download_channel_videos(
                channel_name=channel_name,
                output_dir=output_dir,
                access_token=access_token,
                max_quality=max_quality,
                days_back=days_back,
                use_season_dir=use_season_dir,
                use_channel_dir=use_channel_dir,
                update_metadata=update_metadata,
                include_video=include_video,
                include_audio=include_audio,
                include_images=include_images,
                include_text=include_text,
            )

        all_downloaded.extend(downloaded)

    return all_downloaded