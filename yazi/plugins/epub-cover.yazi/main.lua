--- @since 26.8.15
-- Previewer for .epub: shows the cover image found by cover.py (cached on disk).

local M = {}

local function show_text(job, text)
	ya.preview_widget(job, ui.Text(text):area(job.area):wrap(ui.Wrap.YES))
end

function M:peek(job)
	-- tgdctl browse sets YAZI_CONFIG_HOME; plugins are not told their own directory.
	local config_home = os.getenv("YAZI_CONFIG_HOME") or (os.getenv("HOME") .. "/.config/yazi")
	local script = config_home .. "/plugins/epub-cover.yazi/cover.py"
	local output, err = Command("python3")
		:arg({ "-I", script, tostring(job.file.path) })
		:stdout(Command.PIPED)
		:stderr(Command.PIPED)
		:output()

	if not output then
		return show_text(job, "python3: " .. tostring(err))
	end
	if not output.status.success then
		return show_text(job, (output.stderr ~= "" and output.stderr) or "No cover in this EPUB")
	end

	ya.image_show(Url((output.stdout:gsub("%s+$", ""))), job.area)
end

function M:seek() end

return M
