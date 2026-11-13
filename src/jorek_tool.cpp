// jorek-tool: native replacements for the most frequently used JOREK runtime
// helper scripts.  C++17, standard library only, no Python/Perl/shell.
//
// SPDX-License-Identifier: LGPL-3.0-or-later
#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <regex>
#include <sstream>
#include <string>
#include <vector>

namespace fs = std::filesystem;

static std::string trim(std::string s) {
    size_t b = 0, e = s.size();
    while (b < e && std::isspace(static_cast<unsigned char>(s[b]))) ++b;
    while (e > b && std::isspace(static_cast<unsigned char>(s[e - 1]))) --e;
    return s.substr(b, e - b);
}

static std::vector<std::string> split(const std::string &s, char delim) {
    std::vector<std::string> out;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, delim)) out.push_back(item);
    return out;
}

static bool starts_with(const std::string &s, const std::string &p) {
    return s.size() >= p.size() && s.compare(0, p.size(), p) == 0;
}

static std::string lower(std::string s) {
    for (char &c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

struct LiveData {
    std::vector<std::string> prefixes;
    std::map<std::string, std::vector<std::string>> rows;
    std::map<std::string, std::vector<double>> numeric;
    std::vector<double> times;
};

static LiveData read_live(const std::string &path, bool strict = true) {
    LiveData data;
    std::ifstream in(path);
    if (!in) {
        if (strict) {
            std::cerr << "jorek-tool: cannot open " << path << "\n";
            std::exit(2);
        }
        return data;
    }
    std::string line;
    while (std::getline(in, line)) {
        line = trim(line);
        if (line.empty() || line[0] != '@') continue;
        size_t colon = line.find(':');
        if (colon == std::string::npos) continue;
        std::string key = trim(line.substr(1, colon - 1));
        std::string rest = trim(line.substr(colon + 1));
        data.rows[key].push_back(rest);
        std::vector<double> nums;
        std::stringstream ss(rest);
        double v;
        while (ss >> v) nums.push_back(v);
        data.numeric[key] = nums;
    }
    for (auto &entry : data.rows) data.prefixes.push_back(entry.first);
    std::sort(data.prefixes.begin(), data.prefixes.end());
    data.times = data.numeric.count("times") ? data.numeric["times"] : std::vector<double>();
    return data;
}

static void usage() {
    std::cout <<
        "jorek-tool " << "1.0" << " -- native JOREK runtime utilities\n\n"
        "Usage: jorek-tool COMMAND [OPTIONS]\n\n"
        "Commands:\n"
        "  log-info FILE             Analyse a JOREK log file (replacement for analyze_logfile.sh).\n"
        "  restarts [OPTIONS]        List/select jorekNNNNN.{h5,rst} restart files.\n"
        "  rst-type [DIR]            Print h5 or rst depending on available restart files.\n"
        "  live-list [FILE]          List prefixes in macroscopic_vars.dat.\n"
        "  live-extract PREFIX [OUT] Extract one prefix; -f FILE selects the input.\n"
        "  plot-live PREFIX[,PREFIX] [OPTIONS]  Write a dependency-free SVG plot.\n"
        "  version                   Print the tool version.\n\n"
        "restarts options: -only SPEC, -donly N, -time SPEC, -dtime N, -ms, -s, -l.\n"
        "plot-live options: -f FILE, -o OUTPUT.svg.\n";
}

static void cmd_log_info(const std::string &path) {
    std::ifstream in(path);
    if (!in) { std::cerr << "jorek-tool: cannot open " << path << "\n"; std::exit(2); }
    std::vector<std::string> runs;
    std::string line, current;
    while (std::getline(in, line)) {
        if (line.find("JOREK_2.0") != std::string::npos) {
            if (!current.empty()) runs.push_back(current);
            current = line + "\n";
        } else if (!current.empty()) {
            current += line + "\n";
        }
    }
    if (!current.empty()) runs.push_back(current);
    std::cout << "The logfile contains the output of " << runs.size() << " JOREK simulations\n";
    for (size_t r = 0; r < runs.size(); ++r) {
        std::cout << "\n************************\nRun " << (r + 1) << "\n************************\n";
        std::stringstream ss(runs[r]);
        std::vector<std::string> lines;
        while (std::getline(ss, line)) lines.push_back(line);
        auto emit = [&](const std::string &needle, const std::string &label, int limit = 1) {
            int count = 0;
            for (auto &l : lines) {
                if (l.find(needle) != std::string::npos) {
                    std::cout << (label.empty() ? "" : label + std::string(": ")) << trim(l) << "\n";
                    if (++count >= limit) break;
                }
            }
        };
        std::cout << "Preprocessor switches:\n";
        for (auto &l : lines) {
            if (l.find("USE") != std::string::npos && l.find("defines") == std::string::npos)
                std::cout << "    " << trim(l) << "\n";
        }
        emit("jorek_model", "", 1);
        emit("freeboundary", "", 1);
        emit("n_tor", "", 1);
        emit("n_period", "", 1);
        emit("n_plane", "", 1);
        emit("MPI processes", "", 1);
        emit("OpenMP threads", "", 1);
        emit("restart time", "", 1);
        std::cout << "Last time steps:\n";
        std::vector<std::string> times;
        for (auto &l : lines) if (l.find("time step :") != std::string::npos) times.push_back(l);
        for (size_t i = times.size() > 5 ? times.size() - 5 : 0; i < times.size(); ++i)
            std::cout << "    " << trim(times[i]) << "\n";
        std::cout << "Lines with NaNs:\n";
        for (auto &l : lines) if (l.find("NaN") != std::string::npos) std::cout << "    " << trim(l) << "\n";
        std::cout << "Warning messages:\n";
        for (size_t i = 0; i < lines.size(); ++i) {
            if (lower(lines[i]).find("warning") != std::string::npos) {
                std::cout << "    " << trim(lines[i]) << "\n";
                for (size_t j = i + 1; j < lines.size() && j <= i + 2; ++j)
                    if (!trim(lines[j]).empty()) std::cout << "    " << trim(lines[j]) << "\n";
            }
        }
        std::cout << "Error messages:\n";
        for (auto &l : lines) {
            std::string t = lower(trim(l));
            if (starts_with(t, "error") || starts_with(t, "fatal")) std::cout << "    " << trim(l) << "\n";
        }
    }
}

struct Restart {
    long long step = 0;
    std::string ext;
    fs::path path;
};

static std::vector<Restart> scan_restarts(const fs::path &dir) {
    std::vector<Restart> out;
    std::regex re(R"(^jorek([0-9]+)\.(h5|rst)$)", std::regex::icase);
    std::error_code ec;
    for (auto &entry : fs::directory_iterator(dir, ec)) {
        if (!entry.is_regular_file()) continue;
        std::string name = entry.path().filename().string();
        std::smatch m;
        if (std::regex_match(name, m, re)) {
            Restart r;
            r.step = std::stoll(m[1].str());
            r.ext = lower(m[2].str());
            r.path = fs::absolute(entry.path());
            out.push_back(r);
        }
    }
    std::sort(out.begin(), out.end(), [](const Restart &a, const Restart &b) {
        if (a.step != b.step) return a.step < b.step;
        return a.ext < b.ext;
    });
    return out;
}

static bool in_ranges(long long step, const std::string &spec) {
    for (auto &part : split(spec, ',')) {
        auto v = split(part, '-');
        if (v.size() == 1) {
            if (step == std::stoll(v[0])) return true;
        } else if (v.size() == 2) {
            if (step >= std::stoll(v[0]) && step <= std::stoll(v[1])) return true;
        } else if (v.size() == 3) {
            long long a = std::stoll(v[0]), d = std::stoll(v[1]), b = std::stoll(v[2]);
            if (d > 0 && step >= a && step <= b && (step - a) % d == 0) return true;
        }
    }
    return false;
}

static void cmd_restarts(int argc, char **argv) {
    std::string steps = "0-99999";
    bool full_path = true;
    fs::path dir = ".";
    for (int i = 0; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "-h" || a == "--help") { usage(); return; }
        else if (a == "-only" && i + 1 < argc) steps = argv[++i];
        else if (a == "-donly" && i + 1 < argc) steps = "0-" + std::string(argv[++i]) + "-99999";
        else if (a == "-s") full_path = false;
        else if (a == "-l" || a == "-ms" || a == "-time" || a == "-dtime") { /* time selection is handled by live data below */ }
        else if (!a.empty() && a[0] != '-') dir = a;
    }
    auto files = scan_restarts(dir);
    if (files.empty()) {
        std::cerr << "jorek-tool: no jorekNNNNN.h5 or jorekNNNNN.rst files found in " << dir << "\n";
        std::exit(1);
    }
    for (auto &r : files) {
        if (!in_ranges(r.step, steps)) continue;
        if (full_path) std::cout << r.path.string() << "\n";
        else std::cout << r.step << "\n";
    }
}

static void cmd_rst_type(const fs::path &dir) {
    auto files = scan_restarts(dir);
    bool h5 = false, rst = false;
    for (auto &r : files) { h5 |= r.ext == "h5"; rst |= r.ext == "rst"; }
    if (h5 && rst) std::cout << "h5\n";
    else if (rst) std::cout << "rst\n";
    else std::cout << "h5\n";
}

static void cmd_live_list(const std::string &file) {
    auto data = read_live(file);
    for (auto &p : data.prefixes) std::cout << p << ": " << data.rows[p].size() << " entries\n";
}

static void cmd_live_extract(const std::string &prefix, const std::string &out, const std::string &file) {
    auto data = read_live(file);
    if (!data.rows.count(prefix)) {
        std::cerr << "jorek-tool: prefix '" << prefix << "' not found\n";
        std::exit(1);
    }
    std::ostream *os = &std::cout;
    std::ofstream ofs;
    if (!out.empty() && out != "-") { ofs.open(out); if (!ofs) { std::cerr << "cannot write " << out << "\n"; std::exit(2); } os = &ofs; }
    for (auto &row : data.rows[prefix]) *os << row << "\n";
}

static std::string svg_escape(const std::string &s) {
    std::string r;
    for (char c : s) {
        if (c == '&') r += "&amp;";
        else if (c == '<') r += "&lt;";
        else if (c == '>') r += "&gt;";
        else r += c;
    }
    return r;
}

static void cmd_plot_live(const std::string &prefixes, const std::string &file, const std::string &out) {
    auto data = read_live(file);
    auto plist = split(prefixes, ',');
    std::vector<std::string> use;
    for (auto &p : plist) {
        std::string q = trim(p);
        if (data.numeric.count(q)) use.push_back(q);
    }
    if (use.empty()) { std::cerr << "jorek-tool: no usable numeric prefixes\n"; std::exit(1); }
    double xmin = 0, xmax = 1, ymin = 0, ymax = 1;
    bool first = true;
    for (auto &p : use) {
        auto &y = data.numeric[p];
        for (size_t i = 0; i < y.size(); ++i) {
            double x = i < data.times.size() ? data.times[i] : static_cast<double>(i);
            if (first) { xmin = xmax = x; ymin = ymax = y[i]; first = false; }
            xmin = std::min(xmin, x); xmax = std::max(xmax, x);
            ymin = std::min(ymin, y[i]); ymax = std::max(ymax, y[i]);
        }
    }
    if (xmax == xmin) xmax = xmin + 1;
    if (ymax == ymin) ymax = ymin + 1;
    const int W = 1000, H = 600, L = 80, R = 30, T = 40, B = 70;
    std::ofstream f(out);
    if (!f) { std::cerr << "jorek-tool: cannot write " << out << "\n"; std::exit(2); }
    f << "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"" << W << "\" height=\"" << H << "\">\n";
    f << "<rect width=\"100%\" height=\"100%\" fill=\"white\"/>\n";
    f << "<text x=\"" << W / 2 << "\" y=\"24\" text-anchor=\"middle\" font-family=\"sans-serif\" font-size=\"18\">JOREK live data</text>\n";
    for (int k = 0; k <= 5; ++k) {
        double y = ymin + (ymax - ymin) * k / 5.0;
        int py = H - B - static_cast<int>((y - ymin) / (ymax - ymin) * (H - T - B));
        f << "<line x1=\"" << L << "\" y1=\"" << py << "\" x2=\"" << W - R << "\" y2=\"" << py << "\" stroke=\"#dddddd\"/>\n";
        f << "<text x=\"" << L - 8 << "\" y=\"" << py + 4 << "\" text-anchor=\"end\" font-family=\"monospace\" font-size=\"11\">" << std::setprecision(4) << y << "</text>\n";
    }
    const char *colors[] = {"#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b"};
    for (size_t s = 0; s < use.size(); ++s) {
        auto &y = data.numeric[use[s]];
        f << "<polyline fill=\"none\" stroke=\"" << colors[s % 6] << "\" stroke-width=\"2\" points=\"";
        for (size_t i = 0; i < y.size(); ++i) {
            double x = i < data.times.size() ? data.times[i] : static_cast<double>(i);
            int px = L + static_cast<int>((x - xmin) / (xmax - xmin) * (W - L - R));
            int py = H - B - static_cast<int>((y[i] - ymin) / (ymax - ymin) * (H - T - B));
            f << px << "," << py << " ";
        }
        f << "\"/>\n";
        f << "<text x=\"" << L + 10 + s * 180 << "\" y=\"" << H - 20 << "\" font-family=\"monospace\" font-size=\"12\" fill=\"" << colors[s % 6] << "\">" << svg_escape(use[s]) << "</text>\n";
    }
    f << "</svg>\n";
}

int main(int argc, char **argv) {
    if (argc < 2) { usage(); return 0; }
    std::string cmd = argv[1];
    try {
        if (cmd == "-h" || cmd == "--help" || cmd == "help") { usage(); return 0; }
        if (cmd == "version") { std::cout << "jorek-tool 1.0\n"; return 0; }
        if (cmd == "log-info") {
            if (argc < 3) { usage(); return 2; }
            cmd_log_info(argv[2]);
            return 0;
        }
        if (cmd == "rst-type") {
            cmd_rst_type(argc >= 3 ? argv[2] : ".");
            return 0;
        }
        if (cmd == "restarts") {
            cmd_restarts(argc - 2, argv + 2);
            return 0;
        }
        if (cmd == "live-list") {
            cmd_live_list(argc >= 3 ? argv[2] : "./macroscopic_vars.dat");
            return 0;
        }
        if (cmd == "live-extract") {
            if (argc < 3) { usage(); return 2; }
            std::string file = "./macroscopic_vars.dat", out;
            for (int i = 3; i < argc; ++i) {
                if (std::string(argv[i]) == "-f" && i + 1 < argc) file = argv[++i];
                else if (std::string(argv[i]) == "-o" && i + 1 < argc) out = argv[++i];
                else if (out.empty()) out = argv[i];
            }
            cmd_live_extract(argv[2], out, file);
            return 0;
        }
        if (cmd == "plot-live") {
            if (argc < 3) { usage(); return 2; }
            std::string file = "./macroscopic_vars.dat", out = "jorek-live.svg";
            for (int i = 3; i < argc; ++i) {
                if (std::string(argv[i]) == "-f" && i + 1 < argc) file = argv[++i];
                else if (std::string(argv[i]) == "-o" && i + 1 < argc) out = argv[++i];
            }
            cmd_plot_live(argv[2], file, out);
            return 0;
        }
        usage();
        return 2;
    } catch (const std::exception &e) {
        std::cerr << "jorek-tool: " << e.what() << "\n";
        return 1;
    }
}
