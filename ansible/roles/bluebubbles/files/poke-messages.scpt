try
  tell application "Messages"
    if not running then
      launch
    end if
    set _chatCount to (count of chats)
  end tell
on error
end try
